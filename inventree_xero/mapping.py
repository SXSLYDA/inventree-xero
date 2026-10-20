"""Mapping helpers: metadata links, Xero document normalisation, line ownership, contacts and items.

Line ownership
--------------
InvenTree only holds parts and the order lines that belong to them. Xero documents
can carry extra lines InvenTree knows nothing about (labour, freight, interest...).
Every link stores ``line_ids`` - the Xero LineItemIDs that InvenTree owns. When an
existing Xero document is updated, lines InvenTree does not own are sent back
untouched, so anything added in Xero stays in Xero.
"""

import hashlib
import json
import re

from django.db.models import Q
from django.utils import timezone

from .xero_client import XeroError, parse_xero_date

META_KEY = 'xero'
XERO_TAG_RE = re.compile(r'\s*\[Xero [^\]]*\]')

# A link is "final" once InvenTree must stop editing the Xero document
FINAL_STATUSES = {
    'po': {'BILLED', 'DELETED'},
    'invoice': {'PAID', 'VOIDED', 'DELETED'},
    'quote': {'INVOICED', 'DELETED', 'DECLINED'},
    'bill': {'AUTHORISED', 'PAID', 'VOIDED', 'DELETED'},
}

PO_STATUS_RANK = {'DRAFT': 0, 'SUBMITTED': 1, 'AUTHORISED': 2, 'BILLED': 3}

DOC_KEYS = {
    'po': ('PurchaseOrderID', 'PurchaseOrderNumber'),
    'invoice': ('InvoiceID', 'InvoiceNumber'),
    'quote': ('QuoteID', 'QuoteNumber'),
    'bill': ('InvoiceID', 'InvoiceNumber'),
}

# Line fields that can be sent back to Xero when preserving a Xero-only line
WRITABLE_LINE_KEYS = (
    'LineItemID', 'Description', 'Quantity', 'UnitAmount', 'ItemCode',
    'AccountCode', 'TaxType', 'DiscountRate', 'Tracking',
)


# ---------------------------------------------------------------------- metadata
def xmeta(obj):
    """Return a copy of the object's metadata['xero'] dict."""
    metadata = getattr(obj, 'metadata', None) or {}
    return dict(metadata.get(META_KEY) or {})


def write_meta(obj, xero_values, **extra_fields):
    """Persist metadata['xero'] (plus any extra model fields) without calling save().

    A queryset update avoids model save() side effects and does not re-trigger
    InvenTree events (so our own event handlers are not called again).
    """
    metadata = dict(getattr(obj, 'metadata', None) or {})
    metadata[META_KEY] = xero_values
    fields = {'metadata': metadata, **extra_fields}
    type(obj).objects.filter(pk=obj.pk).update(**fields)
    for key, value in fields.items():
        setattr(obj, key, value)


def payload_hash(payload):
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def tagged_description(description, number):
    """Append '[Xero INV-0001]' so InvenTree's built-in search finds orders by Xero number."""
    base = XERO_TAG_RE.sub('', description or '').strip()
    tag = f'[Xero {number}]'
    room = 250 - len(tag) - 1
    if len(base) > room:
        base = base[: max(room - 1, 0)].rstrip() + '…'
    return f'{base} {tag}'.strip()


def strip_tag(description):
    return XERO_TAG_RE.sub('', description or '').strip()


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money_amount(money):
    """Return the amount of a djmoney Money (or None → 0)."""
    if money is None:
        return 0
    return getattr(money, 'amount', money) or 0


# ---------------------------------------------------------------------- documents
def deep_link(doc_type, xero_id, short_code):
    if not xero_id:
        return ''
    paths = {
        'po': f'/Accounts/Payable/PurchaseOrders/View/{xero_id}',
        'invoice': f'/AccountsReceivable/View.aspx?InvoiceID={xero_id}',
        'bill': f'/AccountsPayable/View.aspx?InvoiceID={xero_id}',
        'quote': f'/Accounts/Receivable/Quotes/View/{xero_id}',
        'contact': f'/Contacts/View/{xero_id}',
        'item': f'/Accounts/Inventory/{xero_id}',
    }
    path = paths[doc_type]
    if short_code:
        return f'https://go.xero.com/organisationlogin/default.aspx?shortcode={short_code}&redirecturl={path}'
    return f'https://go.xero.com{path}'


def doc_info(doc_type, doc, short_code=''):
    """Normalise a Xero PurchaseOrder / Invoice / Bill / Quote into a common dict."""
    id_key, number_key = DOC_KEYS[doc_type]
    contact = doc.get('Contact') or {}
    xero_id = doc.get(id_key) or ''
    due = (
        doc.get('DueDateString') or doc.get('DueDate')
        or doc.get('DeliveryDateString') or doc.get('DeliveryDate')
        or doc.get('ExpiryDateString') or doc.get('ExpiryDate')
    )
    return {
        'doc_type': doc_type,
        'id': xero_id,
        'number': doc.get(number_key) or '',
        'reference': doc.get('Reference') or '',
        'status': (doc.get('Status') or '').upper(),
        'contact_id': contact.get('ContactID') or '',
        'contact_name': contact.get('Name') or '',
        'date': parse_xero_date(doc.get('DateString') or doc.get('Date')),
        'due': parse_xero_date(due),
        'total': doc.get('Total'),
        'amount_due': doc.get('AmountDue'),
        'amount_paid': doc.get('AmountPaid'),
        'currency': doc.get('CurrencyCode') or '',
        'updated': parse_xero_date(doc.get('UpdatedDateUTC')),
        'lines': doc.get('LineItems') or [],
        'url': deep_link(doc_type, xero_id, short_code),
    }


def lines_signature(lines):
    simplified = [
        (ln.get('ItemCode') or '', ln.get('Description') or '', str(ln.get('Quantity')),
         str(ln.get('UnitAmount')), str(ln.get('DiscountRate') or ''))
        for ln in lines
    ]
    return payload_hash(simplified)


def preserved_lines(doc_lines, owned_ids):
    """Lines of a Xero document that InvenTree does not own, ready to send back unchanged."""
    owned = set(owned_ids or [])
    kept = []
    for line in doc_lines or []:
        line_id = line.get('LineItemID')
        if not line_id or line_id in owned:
            continue
        kept.append({k: line[k] for k in WRITABLE_LINE_KEYS if k in line and line[k] not in (None, '', [])})
    return kept


def xero_only_summary(doc_lines, owned_ids):
    """Summary of the lines that live only in Xero (labour, freight, interest...)."""
    owned = set(owned_ids or [])
    others = [ln for ln in doc_lines or [] if ln.get('LineItemID') and ln['LineItemID'] not in owned]
    return {
        'count': len(others),
        'total': round(sum(as_float(ln.get('LineAmount')) for ln in others), 2),
        'lines': [(ln.get('Description') or ln.get('ItemCode') or 'Line')[:80] for ln in others[:8]],
    }


def meta_from_info(info, existing=None, **extra):
    meta = dict(existing or {})
    meta.update({
        'id': info['id'],
        'doc_type': info['doc_type'],
        'number': info['number'],
        'reference': info['reference'],
        'status': info['status'],
        'url': info['url'],
        'total': info['total'],
        'amount_due': info['amount_due'],
        'amount_paid': info['amount_paid'],
        'currency': info['currency'],
        'updated': info['updated'].isoformat() if info['updated'] else meta.get('updated'),
        'synced': timezone.now().isoformat(),
        'final': info['status'] in FINAL_STATUSES[info['doc_type']],
        'error': None,
    })
    meta.update(extra)
    if 'line_ids' in meta and 'xero_only' not in extra:
        meta['xero_only'] = xero_only_summary(info['lines'], meta['line_ids'])
    return meta


# ---------------------------------------------------------------------- contacts
COMPANY_WORDS = {'pty', 'ltd', 'limited', 'the', 'co', 'inc', 'llc', 'proprietary', 'company', 'corp', 'corporation'}


def normalise_name(name):
    words = re.sub(r'[^a-z0-9 ]', ' ', (name or '').lower()).split()
    return ' '.join(w for w in words if w not in COMPANY_WORDS)


def _digits(value):
    return re.sub(r'\D', '', value or '')


def contact_conflicts(company, contact):
    """What on a Xero contact contradicts the InvenTree company (only where both have a value)."""
    conflicts = []
    company_email = (getattr(company, 'email', '') or '').strip().lower()
    contact_email = (contact.get('EmailAddress') or '').strip().lower()
    if company_email and contact_email and company_email != contact_email:
        conflicts.append(f'email ({contact_email} vs {company_email})')
    company_tax = _digits(getattr(company, 'tax_id', ''))
    contact_tax = _digits(contact.get('TaxNumber'))
    if company_tax and contact_tax and company_tax != contact_tax:
        conflicts.append(f'tax number ({contact.get("TaxNumber")} vs {company.tax_id})')
    return conflicts


class ContactMapper:
    """Links InvenTree Companies to Xero Contacts (stored in company.metadata['xero'])."""

    def __init__(self, sync):
        self.sync = sync
        self.client = sync.client

    @property
    def mode(self):
        return self.sync.setting('CONTACT_SYNC') or 'create'

    @staticmethod
    def contact_payload(company):
        payload = {'Name': company.name[:255]}
        if getattr(company, 'email', ''):
            payload['EmailAddress'] = company.email
        if getattr(company, 'phone', ''):
            payload['Phones'] = [{'PhoneType': 'DEFAULT', 'PhoneNumber': company.phone[:50]}]
        if getattr(company, 'website', ''):
            payload['Website'] = company.website
        if getattr(company, 'tax_id', ''):
            payload['TaxNumber'] = company.tax_id[:50]
        address = getattr(company, 'primary_address', None)
        if address:
            payload['Addresses'] = [{
                'AddressType': 'STREET',
                'AddressLine1': (getattr(address, 'line1', '') or '')[:500],
                'AddressLine2': (getattr(address, 'line2', '') or '')[:500],
                'City': (getattr(address, 'postal_city', '') or '')[:255],
                'Region': (getattr(address, 'province', '') or '')[:255],
                'PostalCode': (getattr(address, 'postal_code', '') or '')[:50],
                'Country': (getattr(address, 'country', '') or '')[:50],
            }]
        return payload

    def link(self, company, contact, baseline=True):
        meta = xmeta(company)
        meta.update({
            'contact_id': contact['ContactID'],
            'contact_name': contact.get('Name', ''),
            'url': deep_link('contact', contact['ContactID'], self.client.conn.short_code),
            'synced': timezone.now().isoformat(),
        })
        if baseline:
            # Changes made in InvenTree after this point are pushed (when contact updates are on)
            meta['contact_hash'] = payload_hash(self.contact_payload(company))
        write_meta(company, meta)

    def candidates(self, name):
        """Active Xero contacts whose name matches (exactly, or apart from case/punctuation/Pty Ltd)."""
        target = normalise_name(name)
        return [c for c in self.client.search_contacts(name)
                if c.get('ContactStatus', 'ACTIVE') != 'ARCHIVED' and normalise_name(c.get('Name') or '') == target]

    def ensure(self, company, create=None):
        """Return the Xero ContactID for a company, linking or creating as needed.

        Strict matching (default): a contact is only linked automatically when exactly one Xero
        contact has the same name and its email / ABN don't contradict the company's. Anything
        less certain is held for a person to link on the company page.
        """
        from .safety import Hold

        meta = xmeta(company)
        if meta.get('contact_id'):
            return meta['contact_id']
        if self.sync.preview:
            return 'preview'

        strict = (self.sync.setting('CONTACT_MATCH') or 'strict') == 'strict'
        similar = self.candidates(company.name)
        exact = [c for c in similar if (c.get('Name') or '').strip().casefold() == company.name.strip().casefold()]
        link_page = 'link the right one on the company\'s Xero panel'
        if len(exact) > 1:
            raise Hold(f'{len(exact)} Xero contacts are named "{company.name}" - {link_page}.')
        if exact:
            contact = exact[0]
            if strict:
                conflicts = contact_conflicts(company, contact)
                if conflicts:
                    raise Hold(f'Xero contact "{contact.get("Name")}" has a different {" and ".join(conflicts)} to '
                               f'the InvenTree company - check it and {link_page}.')
            action = 'link'
        elif similar and strict:
            names = ', '.join(f'"{c.get("Name")}"' for c in similar[:3])
            raise Hold(f'No Xero contact is named exactly "{company.name}", but Xero has {names} - '
                       f'{link_page} rather than creating a duplicate.')
        else:
            if create is None:
                create = self.mode in ('create', 'create_update')
            if not create:
                raise Hold(f'No Xero contact named "{company.name}" - {link_page}, '
                           f'or allow contact creation in the plugin settings.')
            if self.client.dry_run:
                self.sync.log('info', 'export', 'contact', 'dry-run', f'Dry run - would create Xero contact {company.name}',
                              ref=company.name, pk=company.pk)
                return f'dry-run-contact:{company.pk}'
            contact = self.client.create_contact(self.contact_payload(company))
            action = 'create'

        self.link(company, contact)
        self.sync.log('info', 'export', 'contact', action, f'{company.name} → Xero contact {contact.get("Name")}',
                      ref=company.name, pk=company.pk, xero_id=contact['ContactID'])
        return contact['ContactID']

    def push_update(self, company):
        """Send changed company details to the linked Xero contact. Returns True if sent."""
        meta = xmeta(company)
        if not meta.get('contact_id'):
            return False
        payload = self.contact_payload(company)
        digest = payload_hash(payload)
        if meta.get('contact_hash') is None:
            meta['contact_hash'] = digest  # first sight: record a baseline, don't overwrite Xero
            write_meta(company, meta)
            return False
        if digest == meta['contact_hash']:
            return False
        self.client.post_document('Contacts', 'Contacts', dict(payload, ContactID=meta['contact_id']))
        meta['contact_hash'] = digest
        meta['synced'] = timezone.now().isoformat()
        write_meta(company, meta)
        self.sync.log('info', 'export', 'contact', 'update', f'Updated Xero contact details for {company.name}',
                      ref=company.name, pk=company.pk, xero_id=meta['contact_id'])
        return True

    def company_for(self, contact_id, contact_name, role):
        """Find (or create) the InvenTree Company for a Xero contact. role: 'supplier'|'customer'."""
        from company.models import Company

        company = None
        if contact_id:
            company = Company.objects.filter(**{f'metadata__{META_KEY}__contact_id': contact_id}).first()
        if not company and contact_name:
            company = Company.objects.filter(name__iexact=contact_name.strip()).first()
            if company and contact_id:
                self.link(company, {'ContactID': contact_id, 'Name': contact_name})
        if not company:
            if not self.sync.flag('AUTO_CREATE_COMPANIES') or not contact_name:
                return None
            company = Company.objects.create(
                name=contact_name.strip()[:100],
                description='Created from Xero',
                is_supplier=(role == 'supplier'),
                is_customer=(role == 'customer'),
                is_manufacturer=False,
            )
            if contact_id:
                self.link(company, {'ContactID': contact_id, 'Name': contact_name})
            self.sync.log('info', 'import', 'contact', 'create', f'Created company {company.name} from Xero contact',
                          ref=company.name, pk=company.pk, xero_id=contact_id)

        flag = 'is_supplier' if role == 'supplier' else 'is_customer'
        if not getattr(company, flag):
            Company.objects.filter(pk=company.pk).update(**{flag: True})
            setattr(company, flag, True)
        return company


# ---------------------------------------------------------------------- items
class ItemMapper:
    """Maps InvenTree Parts / SupplierParts to Xero Item codes and back."""

    MAX_CODE = 30

    def __init__(self, sync):
        self.sync = sync
        self.client = sync.client
        self._codes = None

    @property
    def codes(self):
        if self._codes is None:
            self._codes = {item['Code']: item for item in self.client.get_items() if item.get('Code')}
        return self._codes

    def raw_code(self, part, supplier_part=None):
        source = self.sync.setting('ITEM_CODE_SOURCE') or 'name'
        if source == 'sku' and supplier_part is not None and supplier_part.SKU:
            code = supplier_part.SKU
        elif source == 'ipn' and part.IPN:
            code = part.IPN
        else:
            code = part.name
        code = (code or '').strip()
        return code if 0 < len(code) <= self.MAX_CODE else None

    def item_payload(self, part, code):
        return {
            'Code': code,
            'Name': (part.name or code)[:50],
            'Description': (part.description or part.name or code)[:4000],
            'PurchaseDescription': (part.description or part.name or code)[:4000],
            'IsTrackedAsInventory': False,
            'PurchaseDetails': self.sync.account_details('purchase'),
            'SalesDetails': self.sync.account_details('sales'),
        }

    def link_part(self, part, item):
        meta = xmeta(part)
        if meta.get('item_id') == item.get('ItemID') and meta.get('item_code') == item.get('Code'):
            return
        meta.update({
            'item_id': item.get('ItemID'),
            'item_code': item.get('Code'),
            'url': deep_link('item', item.get('ItemID'), self.client.conn.short_code) if item.get('ItemID') else '',
            'synced': timezone.now().isoformat(),
        })
        write_meta(part, meta)

    def create_item(self, part, code):
        item = self.client.create_item(self.item_payload(part, code))
        self.codes[code] = item
        self.link_part(part, item)
        self.sync.log('info', 'export', 'item', 'create', f'Created Xero item {code}', ref=code, pk=part.pk,
                      xero_id=item.get('ItemID', ''))
        return item

    def code_for(self, part, supplier_part=None):
        """Item code to put on a Xero line, or None (line is then description-only)."""
        if part is None:
            return None
        code = self.raw_code(part, supplier_part)
        if not code:
            return None
        if code in self.codes:
            self.link_part(part, self.codes[code])
            return code
        if not self.sync.flag('CREATE_XERO_ITEMS') or self.sync.preview or self.client.dry_run:
            return None
        try:
            self.create_item(part, code)
        except XeroError as exc:
            self.sync.log('warning', 'export', 'item', 'error', f'Could not create Xero item {code}: {exc}', ref=code)
            return None
        return code

    def find_part(self, code):
        from part.models import Part

        if not code:
            return None
        return Part.objects.filter(Q(name=code) | Q(IPN=code)).order_by('-active', 'pk').first()

    def has_part_match(self, code, salable=False):
        """True if a Xero item code corresponds to something InvenTree holds (no side effects)."""
        from company.models import SupplierPart

        if not code:
            return False
        part = self.find_part(code)
        if part is not None:
            return bool(part.salable) if salable else True
        return (not salable) and SupplierPart.objects.filter(SKU=code).exists()

    def match_supplier_part(self, code, supplier):
        """Xero ItemCode → SupplierPart for this supplier (by SKU, then via the Part)."""
        from company.models import SupplierPart

        if not code:
            return None
        supplier_part = SupplierPart.objects.filter(supplier=supplier, SKU=code).select_related('part').first()
        if supplier_part:
            return supplier_part
        part = self.find_part(code)
        if not part:
            return None
        supplier_part = SupplierPart.objects.filter(supplier=supplier, part=part).first()
        if supplier_part is None and self.sync.flag('AUTO_CREATE_SUPPLIER_PARTS'):
            supplier_part = SupplierPart.objects.create(part=part, supplier=supplier, SKU=code[:100])
            self.sync.log('info', 'import', 'item', 'create',
                          f'Created supplier part {supplier.name} {code} for {part.full_name}', ref=code, pk=part.pk)
        return supplier_part
