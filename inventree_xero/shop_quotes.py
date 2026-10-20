"""Web shop quote requests <-> Xero quotes (needs the inventree-woocommerce plugin).

The shop owns the customer conversation: it emails the quote, shows it in the
customer's account, takes acceptance and payment, and expires quotes nobody
accepts. Xero is where you price and keep the quote. This module keeps the two
in step.

"Price and send in Xero" (SHOP_QUOTES = xero) makes this plugin the shop's quote
pricing provider (see inventree-woocommerce's providers.py): the shop's own
pricing is locked, every Xero line reaches the shop - parts as part lines,
labour / freight / anything without a part as charges - and the customer gets
Xero's PDF. Lines are only ever written to Xero when the quote is created
(placeholders); after that Xero's lines are left exactly as you made them.
The other modes keep pricing in InvenTree and mirror the quote into Xero.

  Shop quote request                 Xero quote
  ------------------                 ----------
  new / waiting / call / reviewing   DRAFT  (placeholder lines until priced)
  quoted (sent to customer)          SENT   (expiry = valid until)
  accepted (paid or on account)      ACCEPTED, then INVOICED once invoiced
  declined / expired / cancelled     DECLINED

And back, when "Xero edits go to the shop" is on:

  Edit lines in Xero      -> shop lines replaced (a sent quote is reopened first)
  Mark the quote Sent     -> the shop sends it to the customer for approval
  Mark it Draft again     -> the shop reopens it (the customer's payment link is cancelled)
  Mark it Declined        -> the shop declines it, with the Xero quote Summary as the reason

Only the shop plugin's public pieces are used: its QuoteRequest / QuoteItem /
QuoteLine models, quotes.apply_action() and config.load_config().
"""

import logging
import re
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone

from .mapping import as_float, doc_info, lines_signature, payload_hash
from .safety import Hold
from .xero_client import DryRunWrite, WritesPaused, XeroError

logger = logging.getLogger('inventree')

SHOP_OPEN = {'new', 'waiting_sample', 'call', 'reviewing'}
SHOP_CLOSED = {'declined', 'expired', 'cancelled'}
XERO_FINAL = {'INVOICED', 'DELETED'}
XERO_RANK = {'DRAFT': 0, 'SENT': 1, 'ACCEPTED': 2, 'DECLINED': 2, 'INVOICED': 3}

ITEM_PREFIX_RE = re.compile(r'^Item (\d+):\s*')
LEAD_TIME_RE = re.compile(r'\s*\(lead time ([^)]{1,60})\)\s*$')
KIND_LABELS = {'custom': 'Custom harness', 'copy': 'Copy of an existing harness', 'component': 'Component'}


def shop_models():
    """(QuoteRequest, QuoteLine) when the shop plugin is installed and active, else None."""
    try:
        from plugin.registry import registry

        if registry.get_plugin('woocommerce') is None:
            return None
        from inventree_woocommerce.models import QuoteLine, QuoteRequest
    except Exception:  # noqa: BLE001 — not installed / app not loaded
        return None
    return QuoteRequest, QuoteLine


class ShopQuoteError(Exception):
    """The shop refused an action (its message is shown in the log and on the quote link)."""


class ShopQuotes:
    def __init__(self, sync):
        self.sync = sync
        self.client = sync.client

    # ------------------------------------------------------------------ settings
    @property
    def mode(self):
        if not self.sync.flag('SHOP_INTEGRATION'):
            return 'off'
        return self.sync.setting('SHOP_QUOTES') or 'xero'

    def available(self):
        return self.mode != 'off' and shop_models() is not None

    @property
    def prices_in_xero(self):
        """Xero is the shop's pricing provider: Xero owns the lines."""
        return self.mode == 'xero'

    @property
    def xero_edits(self):
        return self.prices_in_xero or self.sync.flag('SHOP_QUOTE_XERO_EDITS')

    @staticmethod
    def target_status(shop_status):
        if shop_status in SHOP_OPEN:
            return 'DRAFT'
        return {'quoted': 'SENT', 'accepted': 'ACCEPTED'}.get(shop_status, 'DECLINED')

    # ------------------------------------------------------------------ shop actions
    def shop_action(self, quote, action, **data):
        from inventree_woocommerce.config import load_config
        from inventree_woocommerce.quotes import QuoteError, apply_action

        if self.sync.paused:
            raise WritesPaused('Writes are paused (kill switch)')
        if self.sync.dry_run:
            # Shop actions email the customer: a dry run never does them
            raise DryRunWrite(f'Dry run: would {action} shop quote {quote.reference}')

        try:
            message = apply_action(quote, {'action': action, **data}, load_config(), source='provider')
        except QuoteError as exc:
            raise ShopQuoteError(f'Shop refused "{action}": {exc}') from exc
        quote.refresh_from_db()
        return message

    # ------------------------------------------------------------------ building the Xero quote
    def contact_id(self, quote):
        from company.models import Company

        company = Company.objects.filter(pk=quote.company_id).first() if quote.company_id else None
        if company is not None:
            return self.sync.contacts.ensure(company)
        name = (quote.company_name or quote.name or quote.email or quote.reference).strip()
        matches = [c for c in self.client.search_contacts(name)
                   if (c.get('Name') or '').strip().casefold() == name.casefold()]
        if matches:
            return matches[0]['ContactID']
        payload = {'Name': name[:255]}
        if quote.email:
            payload['EmailAddress'] = quote.email
        if quote.phone:
            payload['Phones'] = [{'PhoneType': 'DEFAULT', 'PhoneNumber': quote.phone[:50]}]
        contact = self.client.create_contact(payload)
        self.sync.log('info', 'export', 'contact', 'create', f'Created Xero contact {name} for quote {quote.reference}',
                      ref=quote.reference, xero_id=contact['ContactID'])
        return contact['ContactID']

    def build_lines(self, quote):
        """(lines, placeholder). Priced shop lines, or one description-only line per requested item."""
        from part.models import Part

        account = self.sync.account_details('sales')
        positions = {item.pk: item.position for item in quote.items.all()}
        priced = list(quote.lines.all().order_by('position'))
        if priced:
            parts = Part.objects.in_bulk([ln.part_id for ln in priced if ln.part_id])
            lines = []
            for ln in priced:
                part = parts.get(ln.part_id) if ln.part_id else None  # no part: labour, freight...
                desc = ln.description or (part.full_name if part else 'Item')
                if ln.item_id in positions:
                    desc = f'Item {positions[ln.item_id]}: {desc}'
                if ln.lead_time:
                    desc = f'{desc} (lead time {ln.lead_time})'
                lines.append(self.sync._line(desc, ln.quantity, ln.unit_price, 0, account,
                                             self.sync.items.code_for(part)))
            return lines, False

        lines = []
        for item in quote.items.all().order_by('position'):
            if item.state == 'cannot':
                continue
            bits = [KIND_LABELS.get(item.kind, item.kind)]
            if item.description:
                bits.append(item.description.strip()[:300])
            if item.oem_part_number:
                bits.append(f'OEM {item.oem_part_number}')
            if item.machine:
                bits.append(f'for {item.machine}')
            lines.append(self.sync._line(f'Item {item.position}: ' + ' - '.join(bits), item.quantity, 0, 0, account))
        return lines, True

    def build_payload(self, quote, link):
        lines, placeholder = self.build_lines(quote)
        target = self.target_status(quote.status)
        current = link.status if link else ''
        if current and XERO_RANK.get(current, 0) > XERO_RANK.get(target, 0) and target != 'DRAFT':
            target = current
        payload = {
            'Contact': {'ContactID': self.contact_id(quote)},
            'Date': (quote.quoted_at or quote.created_at or timezone.now()).date().isoformat(),
            'Reference': quote.reference,
            'Title': f'Quote request {quote.reference}',
            'LineAmountTypes': self.sync.setting('LINE_AMOUNT_TYPES') or 'Exclusive',
            'Status': target,
        }
        lines_owned_by_xero = self.prices_in_xero and link is not None and bool(link.xero_id)
        if lines_owned_by_xero:
            # Priced in Xero: only status, dates and contact go back - never lines or how they're taxed
            del payload['LineAmountTypes']
        if quote.valid_until:
            payload['ExpiryDate'] = quote.valid_until.isoformat()
        if self.sync.flag('SEND_CURRENCY') and getattr(quote, 'currency', ''):
            payload['CurrencyCode'] = quote.currency
        # Placeholder lines go in once, on create. After that a still-unpriced shop quote leaves
        # the Xero lines alone, because that is where someone may be pricing it.
        if lines and not lines_owned_by_xero and (not placeholder or link is None or not link.xero_id):
            payload['LineItems'] = lines
        return payload, lines, placeholder

    # ------------------------------------------------------------------ export
    def export_all(self):
        from .models import XeroQuoteLink

        QuoteRequest, _ = shop_models()
        cutoff = timezone.now() - timedelta(days=self.sync.number('EXPORT_LOOKBACK_DAYS', 180))
        open_links = list(XeroQuoteLink.objects.filter(final=False).values_list('quote_request_id', flat=True))
        for quote in QuoteRequest.objects.filter(Q(updated_at__gte=cutoff) | Q(pk__in=open_links)).order_by('pk'):
            self.safe(self.export_quote, quote)

    def safe(self, func, quote, **kwargs):
        from django.core.cache import cache

        from .models import XeroQuoteLink

        if self.sync.paused:
            return False, 'writes to Xero are paused'
        lock = f'inventree-xero-quote-{quote.pk}'
        if not cache.add(lock, '1', timeout=300):
            return False, 'already being synced'
        self.client.audit_context = {'doc_type': 'quote', 'ref': quote.reference, 'pk': quote.pk}
        self.sync._preview_text = ''
        try:
            return True, func(quote, **kwargs)
        except DryRunWrite as exc:
            self.sync.stats['would_send'] += 1
            self.sync.log('info', 'export', 'quote', 'dry-run',
                          f'Dry run - would {self.sync._preview_text or exc} (quote {quote.reference})',
                          ref=quote.reference)
            return True, f'dry run: {self.sync._preview_text or exc}'
        except WritesPaused as exc:
            return False, str(exc)
        except Hold as exc:
            link, _ = XeroQuoteLink.objects.get_or_create(quote_request_id=quote.pk, defaults={'reference': quote.reference})
            already = link.error == str(exc)
            link.error = str(exc)[:2000]
            link.save(update_fields=['error'])
            self.sync.stats['held'] += 1
            if not already:
                self.sync.log('warning', 'export', 'quote', 'held', str(exc), ref=quote.reference,
                              xero_id=link.xero_id, number=link.number)
            self.sync.alerts.add('held', quote.reference, str(exc))
            return False, f'held: {exc}'
        except (XeroError, ShopQuoteError) as exc:
            link, _ = XeroQuoteLink.objects.get_or_create(quote_request_id=quote.pk, defaults={'reference': quote.reference})
            link.error = str(exc)[:2000]
            link.save(update_fields=['error'])
            self.sync.log('error', 'export', 'quote', 'error', str(exc), ref=quote.reference, xero_id=link.xero_id,
                          number=link.number)
            self.sync.alerts.add('error', quote.reference, str(exc)[:500])
            return False, str(exc)
        finally:
            cache.delete(lock)
            self.client.audit_context = {}

    def export_quote(self, quote, force=False):
        from .models import XeroQuoteLink

        link = XeroQuoteLink.objects.filter(quote_request_id=quote.pk).first()
        if link and link.final and not force:
            return 'final'
        target = self.target_status(quote.status)
        if link is None or not link.xero_id:
            if target == 'DECLINED':
                return 'closed before it reached Xero'
            if target == 'DRAFT' and self.mode == 'sent':
                return 'waiting until the quote is sent'

        payload, lines, placeholder = self.build_payload(quote, link)
        if not lines:
            return 'nothing to quote yet'
        digest = payload_hash(payload)
        if link and link.xero_id and digest == link.hash and not force:
            self.sync.stats['unchanged'] += 1
            return 'unchanged'

        pre_status = None
        if payload['Status'] in ('ACCEPTED', 'DECLINED') and (link is None or link.status in ('', 'DRAFT')):
            pre_status = 'SENT'  # Xero quotes go Sent before Accepted / Declined
        existing = {'id': link.xero_id, 'line_ids': None, 'number': link.number} if link and link.xero_id else {}
        self.sync.safety.check('quote', None, payload, compare_total=False, placeholder=placeholder)
        self.sync._preview_text = self.sync.describe('quote', payload, existing)
        doc, _ = self.sync._send('quote', payload, existing, pre_status=pre_status)
        info = doc_info('quote', doc, self.sync.short_code)
        created = link is None or not link.xero_id
        link = link or XeroQuoteLink(quote_request_id=quote.pk)
        self._record(link, quote, info, hash=digest, shop_sig=lines_signature(lines) if not placeholder else link.shop_sig)
        self.sync.stats['exported'] += 1
        self.sync.log('info', 'export', 'quote', 'create' if created else 'update',
                      f'Quote {quote.reference} ({quote.status}) → Xero {info["number"]} ({info["status"]})'
                      + (' with placeholder lines to price in Xero' if placeholder and created else ''),
                      ref=quote.reference, xero_id=info['id'], number=info['number'])
        self.mark_invoiced(link)
        return f'{info["number"]} {info["status"]}'

    def _record(self, link, quote, info, **fields):
        link.reference = quote.reference
        link.xero_id = info['id']
        link.number = info['number']
        link.status = info['status']
        link.shop_status = quote.status
        link.url = info['url']
        link.total = str(info['total'] if info['total'] is not None else '')
        link.updated = info['updated'] or link.updated
        link.synced = timezone.now()
        link.xero_sig = lines_signature(info['lines'])
        link.sales_order_id = quote.sales_order_id or link.sales_order_id
        link.error = ''
        for key, value in fields.items():
            setattr(link, key, value)
        link.final = info['status'] in XERO_FINAL or (info['status'] == 'DECLINED' and quote.status in SHOP_CLOSED)
        link.save()

    def mark_invoiced(self, link):
        """Once the accepted quote's Sales Order has a Xero invoice, mark the Xero quote Invoiced."""
        from order.models import SalesOrder

        from .mapping import xmeta, write_meta

        if link.status != 'ACCEPTED' or link.invoiced or not link.sales_order_id:
            return
        so = SalesOrder.objects.filter(pk=link.sales_order_id).first()
        meta = xmeta(so) if so else {}
        if not so or meta.get('doc_type') != 'invoice' or not meta.get('id'):
            return
        try:
            doc = self.client.post_document('Quotes', 'Quotes', {'QuoteID': link.xero_id, 'Status': 'INVOICED'})
        except XeroError as exc:
            self.sync.log('warning', 'export', 'quote', 'error', f'Could not mark Xero quote invoiced: {exc}',
                          ref=link.reference, xero_id=link.xero_id, number=link.number)
            return
        link.status = doc.get('Status', 'INVOICED')
        link.invoiced = True
        link.final = True
        link.save(update_fields=['status', 'invoiced', 'final'])
        meta['quote'] = {'reference': link.reference, 'number': link.number, 'url': link.url}
        write_meta(so, meta)

    def link_sales_order(self, quote_pk, sales_order_pk):
        """Shop event: quote accepted and its Sales Order created."""
        from .models import XeroQuoteLink

        QuoteRequest, _ = shop_models()
        quote = QuoteRequest.objects.filter(pk=quote_pk).first()
        if quote is None:
            return
        XeroQuoteLink.objects.filter(quote_request_id=quote.pk).update(sales_order_id=sales_order_pk)
        self.safe(self.export_quote, quote)

    # ------------------------------------------------------------------ import (Xero -> shop)
    def import_all(self):
        from .models import XeroQuoteLink

        started = timezone.now()
        docs = self.client.get_all('Quotes', 'Quotes', modified_since=self.sync.import_since('shop_quote'))
        links = {link.xero_id: link for link in XeroQuoteLink.objects.filter(xero_id__in=[d.get('QuoteID') for d in docs])}
        QuoteRequest, _ = shop_models()
        for doc in docs:
            link = links.get(doc.get('QuoteID'))
            if link is None:
                continue
            quote = QuoteRequest.objects.filter(pk=link.quote_request_id).first()
            if quote is None:
                link.final = True
                link.save(update_fields=['final'])
                continue
            self.safe(self.apply_from_xero, quote, link=link, doc=doc)
        self.sync.set_import_mark('shop_quote', started)

    def pull(self, quote):
        """Manual 'Refresh from Xero' for one quote."""
        from .models import XeroQuoteLink

        link = XeroQuoteLink.objects.filter(quote_request_id=quote.pk).first()
        if link is None or not link.xero_id:
            return self.export_quote(quote, force=True)
        doc = self.client.get_document('quote', link.xero_id)
        return self.apply_from_xero(quote, link=link, doc=doc, force=True)

    def rows_from_xero(self, quote, lines, inclusive=False):
        """Xero quote lines -> shop set_lines rows, plus problems that stop them being applied.

        A line whose item code is a salable InvenTree part becomes a part line; anything else (labour,
        freight, a call-out fee...) becomes a charge with no part. Prices are what Xero will invoice: the
        line amount after any discount, ex tax (tax-inclusive quotes have the tax taken off).
        """
        items = {item.position: item.pk for item in quote.items.all()}
        rows, problems = [], []
        for n, ln in enumerate(lines, start=1):
            qty, unit = as_float(ln.get('Quantity')), as_float(ln.get('UnitAmount'))
            desc = (ln.get('Description') or '').strip()
            if qty == 0 and unit == 0:
                continue  # heading / note
            label = desc[:60] or (ln.get('ItemCode') or f'line {n}')
            if ln.get('LineAmount') is not None:
                amount = as_float(ln.get('LineAmount'))
            else:
                amount = qty * unit * (1 - as_float(ln.get('DiscountRate')) / 100)
            if inclusive:
                amount -= as_float(ln.get('TaxAmount'))
            amount = round(amount, 2)
            if amount < 0 or qty < 0:
                problems.append(f'line {n} "{label}" is negative - the shop can\'t take credit lines; '
                                f'use a discount on the line instead')
                continue
            code = (ln.get('ItemCode') or '').strip()
            part = self.sync.items.find_part(code) if code else None
            if part is not None and not part.salable:
                problems.append(f'line {n} "{label}": item {code} is InvenTree part {part.name}, which isn\'t Salable - '
                                f'mark it Salable, or take the item code off the line to quote it as a charge')
                continue
            if amount == 0:
                if part is not None or ITEM_PREFIX_RE.match(desc):
                    problems.append(f'line {n} "{label}" has no price yet - price it, or delete the line')
                continue  # a no-charge note line ("Free delivery"): it stays on the Xero PDF only
            whole = qty >= 1 and qty == int(qty)
            if part is not None and not whole:
                problems.append(f'line {n} "{label}": the shop needs a whole quantity of at least 1 for a part')
                continue

            item_id = None
            match = ITEM_PREFIX_RE.match(desc)
            if match:
                item_id = items.get(int(match.group(1)))
                desc = desc[match.end():]
            lead_time = ''
            match = LEAD_TIME_RE.search(desc)
            if match:
                lead_time = match.group(1).strip()
                desc = desc[:match.start()]
            desc = desc.strip()
            if part is None and not desc:
                desc = code or 'Charge'
            if whole:
                quantity, unit_price = int(qty), amount / int(qty)
            else:
                # e.g. 1.5 hours of labour: one line for the amount, the hours in its description
                quantity, unit_price = 1, amount
                desc = f'{desc} ({qty:g} x {unit:.2f})'
            row = {'part_id': part.pk if part else None, 'quantity': quantity, 'unit_price': f'{unit_price:.4f}',
                   'description': desc[:250], 'lead_time': lead_time[:60]}
            if item_id:
                row['item_id'] = item_id
            rows.append(row)
        return rows, problems

    def apply_from_xero(self, quote, link, doc, force=False):
        info = doc_info('quote', doc, self.sync.short_code)
        if not force and link.updated and info['updated'] and info['updated'] <= link.updated:
            self.sync.stats['unchanged'] += 1
            return 'unchanged'

        done = []
        problems = []
        if self.xero_edits:
            editable = quote.status in SHOP_OPEN or quote.status == 'quoted'
            lines_changed = lines_signature(info['lines']) != link.xero_sig
            revised = False
            if lines_changed and editable:
                rows, problems = self.rows_from_xero(quote, info['lines'],
                                                     inclusive=doc.get('LineAmountTypes') == 'Inclusive')
                if rows and not problems:
                    if quote.status == 'quoted':
                        self.shop_action(quote, 'reopen')
                        revised = True
                        done.append('reopened the sent quote')
                    self.shop_action(quote, 'set_lines', lines=rows)
                    done.append(f'{len(rows)} line(s) updated from Xero')

            resend = not revised or self.sync.flag('SHOP_QUOTE_AUTO_RESEND')
            if not problems:
                if info['status'] == 'SENT' and quote.status in SHOP_OPEN and resend:
                    valid_days = None
                    if info['due']:
                        valid_days = max((info['due'].date() - date.today()).days, 1)
                    data = {'valid_days': valid_days} if valid_days else {}
                    self.shop_action(quote, 'send', **data)
                    done.append('sent to the customer for approval')
                elif info['status'] == 'DRAFT' and quote.status == 'quoted':
                    self.shop_action(quote, 'reopen')
                    done.append('reopened (payment link cancelled)')
                elif info['status'] == 'DECLINED' and (quote.status in SHOP_OPEN or quote.status == 'quoted'):
                    reason = (doc.get('Summary') or '').strip() or (self.sync.setting('SHOP_QUOTE_DECLINE_REASON') or '').strip()
                    if reason:
                        self.shop_action(quote, 'decline', reason=reason[:2000])
                        done.append('declined — customer told')
                    else:
                        problems.append('declined in Xero but no reason: add one in the quote Summary '
                                        '(the customer sees it), or set a default decline reason')

        # Record what is now in both systems so the next export doesn't push it straight back.
        # A blocked Xero edit keeps the old line signature, so it is retried once the problem is fixed.
        previous_sig = link.xero_sig
        payload, lines, placeholder = self.build_payload(quote, link)
        extra = {'xero_sig': previous_sig} if problems else {}
        self._record(link, quote, info, hash=payload_hash(payload),
                     shop_sig=lines_signature(lines) if not placeholder else link.shop_sig,
                     error='; '.join(problems)[:2000], **extra)
        self.sync.stats['updated'] += 1
        if problems:
            self.sync.log('warning', 'import', 'quote', 'skip', f'Quote {quote.reference}: ' + '; '.join(problems),
                          ref=quote.reference, xero_id=info['id'], number=info['number'])
        elif done:
            self.sync.log('info', 'import', 'quote', 'update', f'Xero {info["number"]} → quote {quote.reference}: '
                          + ', '.join(done), ref=quote.reference, xero_id=info['id'], number=info['number'])
        if self.xero_edits and not problems and quote.status in SHOP_OPEN \
                and info['status'] == 'SENT':
            # Revised in Xero after it was sent: put the Xero quote back to Draft; marking it Sent again
            # is the deliberate step that sends the revision to the customer.
            self.export_quote(quote, force=True)
            self.sync.log('info', 'import', 'quote', 'update',
                          f'Quote {quote.reference} revised from Xero - mark {info["number"]} Sent in Xero to send '
                          f'the revision to the customer', ref=quote.reference, xero_id=info['id'],
                          number=info['number'])
            done.append('waiting for you to mark it Sent again')
        return ', '.join(done) or 'no change for the shop'


# ---------------------------------------------------------------- the shop's quote pricing provider
# inventree-woocommerce looks for these on any active plugin (its providers.py); XeroPlugin passes them on.

def provider_details(plugin):
    """Are the shop's quotes priced in Xero right now? None = no, the shop prices them itself."""
    from InvenTree.helpers import str2bool

    from .models import XeroConnection

    if not str2bool(plugin.get_setting('SHOP_INTEGRATION')) or plugin.get_setting('SHOP_QUOTES') != 'xero':
        return None
    if plugin.get_setting('WRITE_MODE') == 'dry_run':
        return None  # Xero changes never reach the shop in a dry run: price in InvenTree meanwhile
    if not XeroConnection.get_solo().is_connected:
        return None
    return {'name': 'Xero', 'partial_accept': str2bool(plugin.get_setting('SHOP_QUOTE_PARTIAL_ACCEPT')), 'pdf': True}


def provider_info(plugin, quote):
    from .models import XeroQuoteLink

    link = XeroQuoteLink.objects.filter(quote_request_id=quote.pk).first()
    if link is None:
        return None
    return {'number': link.number, 'status': link.status, 'url': link.url, 'error': link.error}


def provider_pdf(plugin, quote):
    """(pdf bytes, file name) of the Xero quote, or None if it isn't in Xero (the shop's own PDF is used)."""
    from . import sync as sync_module
    from .models import XeroQuoteLink

    link = XeroQuoteLink.objects.filter(quote_request_id=quote.pk).first()
    if link is None or not link.xero_id:
        return None
    data = sync_module.XeroClient(plugin).get_pdf('quote', link.xero_id)
    return data, f'{link.number or quote.reference}.pdf'
