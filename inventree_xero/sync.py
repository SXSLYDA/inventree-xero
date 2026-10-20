"""Two-way sync engine.

InvenTree Purchase Orders  <-> Xero Purchase Orders, and received POs -> Xero Bills
InvenTree Sales Orders     <-> Xero Sales Invoices (or Quotes)
InvenTree Companies        <-> Xero Contacts (link, create, optional detail updates, balances)
InvenTree Parts            <-> Xero Items

InvenTree holds parts and their order lines only. Lines that exist only in Xero
(labour, freight, interest, ...) are never imported and are preserved whenever
InvenTree updates the Xero document - see mapping.py "Line ownership".
"""

import logging
import re
import traceback
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from InvenTree.helpers import str2bool

from .mapping import (
    FINAL_STATUSES,
    META_KEY,
    PO_STATUS_RANK,
    ContactMapper,
    ItemMapper,
    as_float,
    doc_info,
    lines_signature,
    meta_from_info,
    money_amount,
    payload_hash,
    preserved_lines,
    strip_tag,
    tagged_description,
    write_meta,
    xero_only_summary,
    xmeta,
)
from .alerts import Alerts
from .safety import Hold, Safety
from .shop_quotes import ShopQuotes, shop_models
from .xero_client import DOC_ENDPOINTS, DOC_ID_KEYS, DryRunWrite, WritesPaused, XeroClient, XeroError, parse_xero_date

logger = logging.getLogger('inventree')

LOCK_KEY = 'inventree-xero-sync-lock'
LOCK_TIMEOUT = 60 * 60
ORDER_LOCK_TIMEOUT = 5 * 60
CANCELLED_NAMES = {'cancelled', 'lost', 'returned'}

SO_AUTHORISE_STAGES = {
    'issued': {'in_progress', 'on_hold', 'shipped', 'complete'},
    'shipped': {'shipped', 'complete'},
    'complete': {'complete'},
}
SO_CANCEL_FROM_XERO = {'invoice': {'VOIDED', 'DELETED'}, 'quote': {'DELETED', 'DECLINED'}}
SO_ISSUE_FROM_XERO = {'invoice': {'AUTHORISED', 'PAID'}, 'quote': {'ACCEPTED', 'INVOICED'}}

BALANCE_FIELDS = ('receivable_outstanding', 'receivable_overdue', 'payable_outstanding', 'payable_overdue')
EVENT_BALANCES = 'xero.balances_updated'


def to_decimal(value):
    try:
        return Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return Decimal('0')


class XeroSync:
    """Runs exports/imports. One instance per sync run (holds caches)."""

    def __init__(self, plugin, client=None):
        self.plugin = plugin
        self.client = client or XeroClient(plugin)
        self.contacts = ContactMapper(self)
        self.items = ItemMapper(self)
        self.shop_quotes = ShopQuotes(self)
        self.safety = Safety(self)
        self.alerts = Alerts(plugin)
        self.preview = False
        self._preview_text = ''
        self.stats = Counter()

    @property
    def dry_run(self):
        return self.client.dry_run

    @property
    def paused(self):
        return self.client.paused

    # ================================================================== settings
    def setting(self, key):
        return self.plugin.get_setting(key)

    def flag(self, key):
        return str2bool(self.plugin.get_setting(key))

    def number(self, key, default):
        try:
            return int(self.plugin.get_setting(key))
        except (TypeError, ValueError):
            return default

    def csv(self, key):
        return [s.strip() for s in (self.setting(key) or '').split(',') if s.strip()]

    def can_export(self, kind):
        return (self.setting(f'{kind.upper()}_SYNC_DIRECTION') or 'both') in ('both', 'export')

    def can_import(self, kind):
        return (self.setting(f'{kind.upper()}_SYNC_DIRECTION') or 'both') in ('both', 'import')

    @property
    def so_doc_type(self):
        return 'quote' if self.setting('SO_XERO_DOCUMENT') == 'QUOTE' else 'invoice'

    @property
    def bills_mode(self):
        return self.setting('BILLS_MODE') or 'off'

    @property
    def import_extra_lines(self):
        return self.setting('NON_PART_LINES') == 'extra'

    @property
    def short_code(self):
        return self.client.conn.short_code

    def account_details(self, kind):
        prefix = 'PURCHASE' if kind == 'purchase' else 'SALES'
        details = {}
        account = (self.setting(f'{prefix}_ACCOUNT_CODE') or '').strip()
        tax = (self.setting(f'{prefix}_TAX_TYPE') or '').strip()
        if account:
            details['AccountCode'] = account
        if tax:
            details['TaxType'] = tax
        return details

    # ================================================================== status helpers
    @staticmethod
    def po_status():
        from order.status_codes import PurchaseOrderStatus

        return PurchaseOrderStatus

    @staticmethod
    def so_status():
        from order.status_codes import SalesOrderStatus

        return SalesOrderStatus

    @staticmethod
    def status_name(enum_cls, value):
        for member in enum_cls:
            if member.value == value:
                return member.name.lower()
        return ''

    @staticmethod
    def status_values(enum_cls, names):
        wanted = {n.lower() for n in names}
        return [m.value for m in enum_cls if m.name.lower() in wanted]

    # ================================================================== logging / errors
    def log(self, level, direction, doc_type, action, message, ref='', pk=None, xero_id='', number=''):
        from .models import XeroSyncLog

        XeroSyncLog.objects.create(
            level=level, direction=direction, doc_type=doc_type, action=action,
            inventree_ref=str(ref or '')[:100], inventree_pk=pk,
            xero_id=str(xero_id or '')[:64], xero_number=str(number or '')[:100],
            message=str(message or ''),
        )
        if level == 'error':
            self.stats['errors'] += 1

    def _fail(self, order, kind, direction, exc):
        meta = xmeta(order)
        target = meta.setdefault('bill', {}) if kind == 'bill' else meta
        target['error'] = str(exc)[:500]
        target['error_at'] = timezone.now().isoformat()
        write_meta(order, meta)
        detail = str(exc) if isinstance(exc, XeroError) else f'{exc}\n{traceback.format_exc(limit=5)}'
        self.log('error', direction, kind, 'error', detail, ref=order.reference, pk=order.pk,
                 xero_id=target.get('id', ''), number=target.get('number', ''))
        self.alerts.add('error', order.reference, str(exc)[:500])

    def _hold(self, order, kind, reason):
        """Not sent: needs a person. Shown on the order, logged and alerted (once per reason)."""
        meta = xmeta(order)
        target = meta.setdefault('bill', {}) if kind == 'bill' else meta
        already = (target.get('held') or {}).get('reason') == reason
        target['held'] = {'reason': reason, 'at': timezone.now().isoformat()}
        write_meta(order, meta)
        self.stats['held'] += 1
        if not already:
            self.log('warning', 'export', kind, 'held', reason, ref=order.reference, pk=order.pk,
                     xero_id=target.get('id', ''), number=target.get('number', ''))
        self.alerts.add('held', order.reference, reason)

    def _dry_run_note(self, order, kind):
        meta = xmeta(order)
        target = meta.setdefault('bill', {}) if kind == 'bill' else meta
        target['dry_run'] = {'at': timezone.now().isoformat(), 'would': self._preview_text}
        target.pop('held', None)
        write_meta(order, meta)
        self.stats['would_send'] += 1
        self.log('info', 'export', kind, 'dry-run', f'Dry run - would {self._preview_text}', ref=order.reference,
                 pk=order.pk)

    def safe_export(self, func, order, kind, force=False):
        """Run one export under a per-order lock so events and the schedule can't double-post."""
        if self.paused:
            return False, 'writes to Xero are paused'
        lock = f'inventree-xero-{kind}-{order.pk}'
        if not cache.add(lock, '1', timeout=ORDER_LOCK_TIMEOUT):
            return False, 'already being synced — try again shortly'
        self.client.audit_context = {'doc_type': kind, 'ref': order.reference, 'pk': order.pk}
        self._preview_text = ''
        try:
            order.refresh_from_db()
            return True, func(order, force=force)
        except Hold as exc:
            self._hold(order, kind, str(exc))
            return False, f'held: {exc}'
        except DryRunWrite:
            self._dry_run_note(order, kind)
            return True, f'dry run: would {self._preview_text}'
        except WritesPaused as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — keep syncing other orders, record the failure
            if not isinstance(exc, XeroError):
                logger.exception('Xero export failed for %s', order)
            self._fail(order, kind, 'export', exc)
            return False, str(exc)
        finally:
            cache.delete(lock)
            self.client.audit_context = {}

    def safe_import(self, func, info, *args, **kwargs):
        try:
            return True, func(info, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, XeroError):
                logger.exception('Xero import failed for %s', info.get('number'))
            self.log('error', 'import', info['doc_type'], 'error', f'{exc}', xero_id=info['id'], number=info['number'])
            return False, str(exc)

    def safe_step(self, func, label):
        """Run a whole sync step; a failure is logged and the next step still runs."""
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, XeroError):
                logger.exception('Xero %s failed', label)
            self.log('error', 'system', 'system', 'error', f'{label} failed: {exc}')
            self.alerts.add('error', label, str(exc)[:500])

    # ================================================================== bookkeeping
    def _conn_qs(self):
        from .models import XeroConnection

        return XeroConnection.objects.filter(pk=self.client.conn.pk)

    def get_mark(self, key):
        marks = self._conn_qs().values_list('import_marks', flat=True).first() or {}
        return parse_xero_date(marks.get(key))

    def import_since(self, key):
        mark = self.get_mark(key)
        if mark:
            return mark - timedelta(minutes=5)
        return timezone.now() - timedelta(days=self.number('IMPORT_SINCE_DAYS', 30))

    def set_import_mark(self, key, when):
        marks = dict(self._conn_qs().values_list('import_marks', flat=True).first() or {})
        marks[key] = when.isoformat()
        self._conn_qs().update(import_marks=marks)

    def prune_log(self):
        from .models import XeroSyncLog

        days = self.number('LOG_RETENTION_DAYS', 60)
        XeroSyncLog.objects.filter(created__lt=timezone.now() - timedelta(days=days)).delete()
        from .models import XeroWriteAudit

        keep = self.number('AUDIT_RETENTION_DAYS', 365)
        XeroWriteAudit.objects.filter(created__lt=timezone.now() - timedelta(days=keep)).delete()

    def save_link(self, order, info, existing=None, **extra):
        """Store the Xero link on an order (metadata, link field, searchable description tag)."""
        current = xmeta(order)
        meta = meta_from_info(info, existing if existing is not None else current, **extra)
        meta.pop('held', None)
        meta.pop('dry_run', None)
        if 'bill' in current and 'bill' not in meta:
            meta['bill'] = current['bill']
        fields = {}
        if self.flag('SET_ORDER_LINK') and info['url'] and (not order.link or 'go.xero.com' in order.link):
            fields['link'] = info['url'][:2000]
        if self.flag('TAG_DESCRIPTION') and info['number']:
            new_desc = tagged_description(order.description, info['number'])
            if new_desc != order.description:
                fields['description'] = new_desc
        write_meta(order, meta, **fields)
        return meta

    @staticmethod
    def order_date(order):
        return getattr(order, 'issue_date', None) or getattr(order, 'creation_date', None) or date.today()

    def currency_field(self, order, payload):
        if self.flag('SEND_CURRENCY'):
            currency = getattr(order, 'currency', None)
            if currency:
                payload['CurrencyCode'] = str(currency)

    @staticmethod
    def transition(func):
        """Call an InvenTree order transition; invalid transitions are ignored."""
        try:
            func()
        except ValidationError as exc:
            logger.info('Xero: order transition skipped: %s', exc)

    # ================================================================== run
    def run(self, direction='all', triggered_by='schedule'):
        """Full sync. direction: 'all' | 'export' | 'import'."""
        if self.paused:
            return {'ok': True, 'message': 'Writes to Xero are paused - nothing was synced', 'stats': {}}
        if not cache.add(LOCK_KEY, '1', timeout=LOCK_TIMEOUT):
            return {'ok': False, 'message': 'A Xero sync is already running', 'stats': {}}

        self._conn_qs().update(last_sync_started=timezone.now())
        ok, message = True, ''
        try:
            self.client.ensure_token()
            if direction in ('all', 'export'):
                if self.can_export('po'):
                    self.safe_step(self.export_purchase_orders, 'Purchase order export')
                if self.bills_mode != 'off':
                    self.safe_step(self.export_bills, 'Bill export')
                if self.can_export('so'):
                    self.safe_step(self.export_sales_orders, 'Sales order export')
                if self.shop_quotes.available():
                    self.safe_step(self.shop_quotes.export_all, 'Shop quote export')
                if self.contacts.mode == 'create_update':
                    self.safe_step(self.export_contacts, 'Contact update')
            if direction in ('all', 'import'):
                if self.can_import('po'):
                    self.safe_step(self.import_purchase_orders, 'Purchase order import')
                if self.bills_mode != 'off' or self.flag('LINK_XERO_BILLS'):
                    self.safe_step(self.import_bills, 'Bill import')
                if self.can_import('so'):
                    self.safe_step(self.import_sales_orders, 'Sales import')
                if self.shop_quotes.available():
                    self.safe_step(self.shop_quotes.import_all, 'Shop quote import')
                if self.flag('BALANCE_SYNC'):
                    self.safe_step(self.import_balances, 'Balance import')
            message = self.summary()
            if self.stats['errors']:
                message += ' — see log for errors'
        except Exception as exc:  # noqa: BLE001
            ok = False
            message = str(exc)
            logger.exception('Xero sync failed')
            self.log('error', 'system', 'system', 'error', f'Sync failed: {exc}')
        finally:
            self._conn_qs().update(last_sync_finished=timezone.now(), last_sync_ok=ok and not self.stats['errors'],
                                   last_sync_message=message[:2000])
            cache.delete(LOCK_KEY)
            self.prune_log()

        self.log('info' if ok else 'error', 'system', 'system', 'sync',
                 f'Sync ({triggered_by}, {direction}{", DRY RUN" if self.dry_run else ""}) finished: {message}')
        if not ok:
            self.alerts.add('error', 'sync', message[:500])
        self.alerts.send()
        return {'ok': ok, 'message': message, 'stats': dict(self.stats)}

    def summary(self):
        s = self.stats
        text = (f"exported {s['exported']}, imported {s['imported']}, updated {s['updated']}, "
                f"bills {s['bills']}, unchanged {s['unchanged']}, skipped {s['skipped']}, held {s['held']}, "
                f"errors {s['errors']}")
        if self.dry_run:
            text = f"DRY RUN - would send {s['would_send']}; " + text
        return text

    # ================================================================== single-order entry points
    def _order(self, kind, pk):
        from order.models import PurchaseOrder, SalesOrder

        return (PurchaseOrder if kind in ('po', 'bill') else SalesOrder).objects.get(pk=pk)

    def export_single(self, kind, pk, force=False):
        """Export one order (manual push or event). kind: 'po' | 'so' | 'bill'."""
        if self.paused:
            return False, 'writes to Xero are paused'
        order = self._order(kind, pk)
        if kind == 'bill':
            if self.bills_mode == 'off' and not force:
                return False, 'bills are turned off'
            self.client.ensure_token()
            result = self.safe_export(self.export_bill, order, 'bill', force=force)
        elif not force and not self.can_export(kind):
            return False, 'export disabled for this order type'
        else:
            self.client.ensure_token()
            func = self.export_purchase_order if kind == 'po' else self.export_sales_order
            result = self.safe_export(func, order, kind, force=force)
        self.alerts.send()
        return result

    def preview_single(self, kind, pk):
        """What would be sent for one order, and anything that would stop it - without writing anything."""
        order = self._order(kind, pk)
        self.preview = True
        result = {'kind': kind, 'write_mode': self.client.write_mode, 'problems': [], 'payload': None}
        try:
            meta = xmeta(order)
            link = (meta.get('bill') or {}) if kind == 'bill' else meta
            if kind == 'po':
                payload, note = self.plan_purchase_order(order)
                doc_type, compare = 'po', True
            elif kind == 'bill':
                payload, note = self.plan_bill(order)
                doc_type, compare = 'bill', False
            else:
                payload, note, doc_type = self.plan_sales_order(order)
                compare = True
            if payload is None:
                result['note'] = note
                return result
            try:
                self.safety.approved_in_xero(doc_type, link.get('status', ''), payload)
            except Hold as exc:
                result['problems'].append(str(exc))
            result['problems'] += self.safety.problems('so' if kind == 'so' else kind, order, payload,
                                                       compare_total=compare)
            result['action'] = self.describe(doc_type, payload, link)
            result['unchanged'] = bool(link.get('id')) and payload_hash(payload) == link.get('hash')
            result['payload'] = {
                'status': payload.get('Status'),
                'lines': [{'description': ln.get('Description'), 'item_code': ln.get('ItemCode'),
                           'quantity': ln.get('Quantity'), 'unit_amount': ln.get('UnitAmount'),
                           'discount': ln.get('DiscountRate'), 'account': ln.get('AccountCode'),
                           'tax_type': ln.get('TaxType')} for ln in payload.get('LineItems') or []],
                'total': self.safety.line_total(payload.get('LineItems') or []),
                'reference': payload.get('Reference'),
                'contact_linked': payload.get('Contact', {}).get('ContactID') not in (None, 'preview'),
            }
        except Hold as exc:
            result['problems'].append(str(exc))
        finally:
            self.preview = False
        return result

    def pull_single(self, kind, pk):
        """Re-read one linked order (and its bill) from Xero and apply it."""
        order = self._order(kind, pk)
        meta = xmeta(order)
        results = []
        if meta.get('id'):
            doc_type = meta.get('doc_type') or ('po' if kind == 'po' else self.so_doc_type)
            info = doc_info(doc_type, self.client.get_document(doc_type, meta['id']), self.short_code)
            func = self.update_po_from_xero if kind == 'po' else self.update_so_from_xero
            results.append(self.safe_import(func, info, order, force=True))
        bill = meta.get('bill') or {}
        if kind == 'po' and bill.get('id'):
            info = doc_info('bill', self.client.get_document('bill', bill['id']), self.short_code)
            results.append(self.safe_import(self.import_bill, info, force=True))
        if not results:
            return False, 'Order is not linked to Xero yet'
        ok = all(r[0] for r in results)
        return ok, '; '.join(str(r[1]) for r in results)

    # ================================================================== sending (shared)
    def _send(self, doc_type, payload, link, pre_status=None):
        """Create or update a Xero document.

        On updates, Xero-only lines (not in link['line_ids']) are fetched and sent back
        untouched. Returns (doc, owned_line_ids).
        """
        path, key = DOC_ENDPOINTS[doc_type]
        id_key = DOC_ID_KEYS[doc_type]
        body = dict(payload)
        kept = []
        if link.get('id'):
            body[id_key] = link['id']
            if 'LineItems' in payload:
                current = self.client.get_document(doc_type, link['id'])
                current_lines = current.get('LineItems') or []
                owned = link.get('line_ids')
                if owned is None:
                    owned = [ln.get('LineItemID') for ln in current_lines]
                kept = preserved_lines(current_lines, owned)
                body['LineItems'] = list(payload['LineItems']) + kept
        if pre_status:
            first = self.client.post_document(path, key, dict(body, Status=pre_status))
            body[id_key] = first[id_key]
        doc = self.client.post_document(path, key, body)
        if 'LineItems' not in payload:
            return doc, link.get('line_ids') or []
        kept_ids = {ln['LineItemID'] for ln in kept}
        owned_ids = [ln['LineItemID'] for ln in doc.get('LineItems') or []
                     if ln.get('LineItemID') and ln['LineItemID'] not in kept_ids]
        return doc, owned_ids

    def describe(self, doc_type, payload, link):
        label = {'po': 'purchase order', 'invoice': 'invoice', 'bill': 'bill', 'quote': 'quote'}[doc_type]
        action = f'update {label} {link.get("number") or ""}'.strip() if link.get('id') else f'create {label}'
        lines = payload.get('LineItems')
        if lines is None:
            return f'{action}: set status {payload.get("Status")}'
        return (f'{action}: {len(lines)} line(s), {self.safety.line_total(lines):.2f} '
                f'{payload.get("LineAmountTypes", "")} ({payload.get("Status")})')

    def guarded_send(self, kind, doc_type, order, payload, link, compare_total=True, placeholder=False,
                     pre_status=None):
        """Safety checks, then send (or, in a dry run, record what would be sent)."""
        self.safety.approved_in_xero(doc_type, link.get('status', ''), payload)
        self.safety.check(kind, order, payload, compare_total=compare_total, placeholder=placeholder)
        self._preview_text = self.describe(doc_type, payload, link)
        return self._send(doc_type, payload, link, pre_status=pre_status)

    def _line(self, description, quantity, unit, discount, account, code=None):
        line = {
            'Description': description[:4000] or 'Item',
            'Quantity': as_float(quantity),
            'UnitAmount': round(as_float(unit), 4),
            **account,
        }
        if as_float(discount) > 0:
            line['DiscountRate'] = round(as_float(discount), 2)
        if code:
            line['ItemCode'] = code
        return line

    def _po_line_description(self, li):
        sp = li.part
        part = sp.part if sp else None
        bits = []
        if sp and sp.SKU:
            bits.append(sp.SKU)
        if part:
            if not sp or part.full_name != sp.SKU:
                bits.append(part.full_name)
            if part.description:
                bits.append(part.description)
        mp = getattr(sp, 'manufacturer_part', None) if sp else None
        if mp and mp.MPN and mp.MPN != sp.SKU:
            bits.append(f'MPN {mp.MPN}')
        if li.reference:
            bits.append(f'Ref {li.reference}')
        return ' - '.join(bits)

    def _extra_lines(self, order, account):
        return [
            self._line(' - '.join(x for x in [el.reference, el.description] if x) or 'Extra',
                       as_float(el.quantity) or 1, money_amount(el.price), el.discount, account)
            for el in order.extra_lines.all()
        ]

    # ================================================================== EXPORT: purchase orders
    def export_purchase_orders(self):
        from order.models import PurchaseOrder

        wanted = self.status_values(self.po_status(), self.csv('PO_EXPORT_STATUSES'))
        cutoff = date.today() - timedelta(days=self.number('EXPORT_LOOKBACK_DAYS', 180))
        orders = PurchaseOrder.objects.filter(
            Q(status__in=wanted, creation_date__gte=cutoff) | Q(**{f'metadata__{META_KEY}__has_key': 'id'})
        ).select_related('supplier').order_by('pk')
        for po in orders:
            if xmeta(po).get('final'):
                continue
            self.safe_export(self.export_purchase_order, po, 'po')

    def po_lines(self, po, received_only=False):
        account = self.account_details('purchase')
        lines = []
        for li in po.lines.all().select_related('part', 'part__part', 'part__manufacturer_part'):
            quantity = li.received if received_only else li.quantity
            if received_only and as_float(quantity) <= 0:
                continue
            sp = li.part
            code = self.items.code_for(sp.part if sp else None, sp)
            lines.append(self._line(self._po_line_description(li), quantity, money_amount(li.purchase_price),
                                    li.discount, account, code))
        return lines

    def plan_purchase_order(self, po):
        """Return (payload, note). payload is None when nothing should be sent."""
        meta = xmeta(po)
        name = self.status_name(self.po_status(), po.status)
        current = meta.get('status') or ''
        linked = bool(meta.get('id'))

        if current in FINAL_STATUSES['po']:
            return None, f'Xero PO is {current}'
        if name in CANCELLED_NAMES:
            if not linked:
                return None, 'cancelled and not in Xero'
            if (meta.get('xero_only') or {}).get('count'):
                return None, 'the Xero PO has lines added in Xero — cancel it in Xero'
            self.safety.destructive('po', meta.get('number'), 'Delete')
            return {'Status': 'DELETED'}, 'DELETED'

        lines = self.po_lines(po) + self._extra_lines(po, self.account_details('purchase'))
        if not lines:
            return None, 'no line items'
        contact_id = self.contacts.ensure(po.supplier)

        target = 'DRAFT' if name == 'pending' else (self.setting('PO_PLACED_XERO_STATUS') or 'AUTHORISED')
        target = self.safety.cap('po', target)
        if current and PO_STATUS_RANK.get(current, 0) > PO_STATUS_RANK.get(target, 0):
            target = current  # Xero never lets a PO move backwards

        payload = {
            'Contact': {'ContactID': contact_id},
            'Date': self.order_date(po).isoformat(),
            'LineAmountTypes': self.setting('LINE_AMOUNT_TYPES') or 'Exclusive',
            'Reference': (po.supplier_reference or '')[:255],
            'Status': target,
            'LineItems': lines,
        }
        if po.target_date:
            payload['DeliveryDate'] = po.target_date.isoformat()
        if self.flag('PO_USE_INVENTREE_REFERENCE'):
            payload['PurchaseOrderNumber'] = po.reference
        self.currency_field(po, payload)
        return payload, target

    def export_purchase_order(self, po, force=False):
        meta = xmeta(po)
        payload, note = self.plan_purchase_order(po)
        if payload is None:
            return self._skip(po, 'po', meta, note, force)

        digest = payload_hash(payload)
        if meta.get('id') and digest == meta.get('hash') and not force:
            self.stats['unchanged'] += 1
            return 'unchanged'

        doc, owned = self.guarded_send('po', 'po', po, payload, meta)
        info = doc_info('po', doc, self.short_code)
        action = 'update' if meta.get('id') else 'create'
        self.save_link(po, info, meta, hash=digest, line_ids=owned, origin=meta.get('origin', 'inventree'))
        self.stats['exported'] += 1
        self.log('info', 'export', 'po', action, f'{po.reference} → Xero {info["number"]} ({info["status"]})',
                 ref=po.reference, pk=po.pk, xero_id=info['id'], number=info['number'])
        return f'{action}d {info["number"]} ({info["status"]})'

    def _skip(self, order, kind, meta, note, force):
        doc_type = meta.get('doc_type') or kind
        if meta.get('id') and meta.get('status') in FINAL_STATUSES.get(doc_type, ()) and not meta.get('final'):
            meta['final'] = True
            write_meta(order, meta)
        self.stats['skipped'] += 1
        if force or note.startswith('the Xero'):
            self.log('warning' if note.startswith(('the Xero', 'invoice has')) else 'info', 'export', kind, 'skip',
                     f'Not sent: {note}', ref=order.reference, pk=order.pk)
        return f'skipped: {note}'

    # ================================================================== EXPORT: bills
    def export_bills(self):
        from order.models import PurchaseOrder

        status = self.po_status()
        wanted = self.status_values(status, ['placed', 'on_hold', 'complete'])
        cutoff = date.today() - timedelta(days=self.number('EXPORT_LOOKBACK_DAYS', 180))
        orders = PurchaseOrder.objects.filter(
            Q(status__in=wanted, creation_date__gte=cutoff) | Q(**{f'metadata__{META_KEY}__bill__has_key': 'id'})
        ).select_related('supplier').order_by('pk')
        for po in orders:
            bill = xmeta(po).get('bill') or {}
            if bill.get('final') or bill.get('origin') == 'xero':
                continue
            self.safe_export(self.export_bill, po, 'bill')

    @staticmethod
    def bill_reference(po):
        ref = po.reference
        if po.supplier_reference:
            ref = f'{ref} / {po.supplier_reference}'
        return ref[:255]

    def plan_bill(self, po):
        bill = xmeta(po).get('bill') or {}
        name = self.status_name(self.po_status(), po.status)
        if bill.get('status') in FINAL_STATUSES['bill']:
            return None, f'bill is {bill["status"]} in Xero'
        if bill.get('origin') == 'xero':
            return None, 'bill was created in Xero — not changed by InvenTree'
        if name in CANCELLED_NAMES:
            if bill.get('id') and bill.get('status') == 'DRAFT' and not (bill.get('xero_only') or {}).get('count'):
                self.safety.destructive('bill', bill.get('number'), 'Delete')
                return {'Status': 'DELETED'}, 'DELETED'
            return None, 'purchase order cancelled'
        complete = name == 'complete'
        if self.bills_mode == 'on_complete' and not complete:
            return None, 'waiting for the purchase order to be completed'

        account = self.account_details('purchase')
        lines = self.po_lines(po, received_only=True)
        if not lines:
            return None, 'nothing received yet'
        lines += self._extra_lines(po, account)
        contact_id = self.contacts.ensure(po.supplier)

        status = self.safety.cap('bill', (self.setting('BILL_STATUS') or 'DRAFT') if complete else 'DRAFT')
        bill_date = (getattr(po, 'complete_date', None) if complete else None) or date.today()
        if bill.get('date'):
            bill_date = date.fromisoformat(bill['date'])  # keep the first bill date stable
        payload = {
            'Type': 'ACCPAY',
            'Contact': {'ContactID': contact_id},
            'Date': bill_date.isoformat(),
            'DueDate': (bill_date + timedelta(days=self.number('BILL_DUE_DAYS', 30))).isoformat(),
            'LineAmountTypes': self.setting('LINE_AMOUNT_TYPES') or 'Exclusive',
            'Reference': self.bill_reference(po),
            'Status': status,
            'LineItems': lines,
        }
        self.currency_field(po, payload)
        return payload, status

    def export_bill(self, po, force=False):
        meta = xmeta(po)
        bill = meta.get('bill') or {}
        payload, note = self.plan_bill(po)
        if payload is None:
            if bill.get('id') and bill.get('status') in FINAL_STATUSES['bill'] and not bill.get('final'):
                bill['final'] = True
                meta['bill'] = bill
                write_meta(po, meta)
            self.stats['skipped'] += 1
            if force:
                self.log('info', 'export', 'bill', 'skip', f'Bill not sent: {note}', ref=po.reference, pk=po.pk)
            return f'skipped: {note}'

        digest = payload_hash(payload)
        if bill.get('id') and digest == bill.get('hash') and not force:
            self.stats['unchanged'] += 1
            return 'unchanged'

        doc, owned = self.guarded_send('bill', 'bill', po, payload, bill, compare_total=False)
        info = doc_info('bill', doc, self.short_code)
        action = 'update' if bill.get('id') else 'create'
        meta = xmeta(po)
        meta['bill'] = meta_from_info(info, bill, hash=digest, line_ids=owned, origin='inventree',
                                      date=payload.get('Date', bill.get('date')))
        meta['bill'].pop('held', None)
        meta['bill'].pop('dry_run', None)
        write_meta(po, meta)
        self.stats['bills'] += 1
        self.log('info', 'export', 'bill', action, f'{po.reference} → Xero bill {info["number"] or ""} ({info["status"]})',
                 ref=po.reference, pk=po.pk, xero_id=info['id'], number=info['number'])
        self.mark_po_billed(po)
        return f'bill {action}d ({info["status"]})'

    def mark_po_billed(self, po):
        """Once the PO is complete and billed, mark the linked Xero PO as Billed."""
        if not self.flag('MARK_PO_BILLED'):
            return
        meta = xmeta(po)
        if not meta.get('id') or meta.get('status') in ('BILLED', 'DELETED') or not (meta.get('bill') or {}).get('id'):
            return
        if self.status_name(self.po_status(), po.status) != 'complete':
            return
        try:
            doc = self.client.post_document('PurchaseOrders', 'PurchaseOrders',
                                            {'PurchaseOrderID': meta['id'], 'Status': 'BILLED'})
        except XeroError as exc:
            self.log('warning', 'export', 'po', 'error', f'Could not mark Xero PO billed: {exc}',
                     ref=po.reference, pk=po.pk, xero_id=meta['id'], number=meta.get('number'))
            return
        self.save_link(po, doc_info('po', doc, self.short_code), meta)

    # ================================================================== EXPORT: sales orders
    def export_sales_orders(self):
        from order.models import SalesOrder

        wanted = self.status_values(self.so_status(), self.csv('SO_EXPORT_STATUSES'))
        cutoff = date.today() - timedelta(days=self.number('EXPORT_LOOKBACK_DAYS', 180))
        orders = SalesOrder.objects.filter(
            Q(status__in=wanted, creation_date__gte=cutoff) | Q(**{f'metadata__{META_KEY}__has_key': 'id'})
        ).select_related('customer').order_by('pk')
        for so in orders:
            if xmeta(so).get('final'):
                continue
            self.safe_export(self.export_sales_order, so, 'so')

    def so_lines(self, so):
        account = self.account_details('sales')
        lines = []
        for li in so.lines.all().select_related('part'):
            part = li.part
            bits = []
            if part:
                bits.append(part.full_name)
                if part.description:
                    bits.append(part.description)
            if li.reference:
                bits.append(f'Ref {li.reference}')
            lines.append(self._line(' - '.join(bits), li.quantity, money_amount(li.sale_price),
                                    li.discount, account, self.items.code_for(part)))
        return lines + self._extra_lines(so, account)

    def so_should_authorise(self, status_name):
        mode = self.setting('SO_AUTHORISE_ON') or 'never'
        return status_name in SO_AUTHORISE_STAGES.get(mode, set())

    @staticmethod
    def so_reference(so):
        ref = so.reference
        if so.customer_reference:
            ref = f'{ref} / {so.customer_reference}'
        return ref[:255]

    # ---------------------------------------------------------------- shop (inventree-woocommerce)
    def shop_info(self, so):
        """Details of a WooCommerce web order behind this Sales Order, or None.

        Works from the Sales Order's metadata['woocommerce'] (written by the
        inventree-woocommerce plugin) and, when that plugin is installed, its
        WooOrder record (payment method, transaction id, totals).
        """
        if not self.flag('SHOP_INTEGRATION'):
            return None
        woo = (getattr(so, 'metadata', None) or {}).get('woocommerce')
        if not woo:
            return None
        info = {
            'order_id': woo.get('order_id'),
            'number': woo.get('number'),
            'on_account': bool(woo.get('on_account')),
            'po_number': woo.get('po_number'),
        }
        try:
            from inventree_woocommerce.models import WooOrder

            record = WooOrder.objects.filter(sales_order_id=so.pk).first()
        except Exception:  # noqa: BLE001 — shop plugin not installed / app not loaded
            record = None
        if record is not None:
            payload = getattr(record, 'payload', None) or {}
            info.update({
                'on_account': bool(getattr(record, 'on_account', info['on_account'])),
                'total': payload.get('total'),
                'transaction_id': payload.get('transaction_id') or '',
                'date_paid': payload.get('date_paid') or payload.get('date_paid_gmt') or '',
                'payment_method': payload.get('payment_method_title') or '',
            })
        return info

    def record_shop_payment(self, so, info, shop, meta):
        """Apply the online shop payment to the Xero invoice (once)."""
        account = (self.setting('SHOP_PAYMENT_ACCOUNT') or '').strip()
        if not account or not self.safety.auto_approve or shop.get('on_account') or meta.get('payment_recorded'):
            return
        if info['doc_type'] != 'invoice' or info['status'] != 'AUTHORISED' or as_float(info['amount_due']) <= 0:
            return
        due = as_float(info['amount_due'])
        amount = due
        if shop.get('total') not in (None, ''):
            shop_total = as_float(shop['total'])
            if abs(shop_total - as_float(info['total'])) > 0.01:
                self.log('warning', 'export', 'so', 'payment',
                         f'Shop total {shop_total:.2f} differs from Xero invoice total {as_float(info["total"]):.2f} '
                         f'(tax or rounding) — recorded {min(shop_total, due):.2f}, check the invoice',
                         ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])
            amount = min(shop_total, due)
        paid_on = (shop.get('date_paid') or '')[:10] or date.today().isoformat()
        reference = ' '.join(x for x in [shop.get('payment_method'), shop.get('transaction_id')] if x)[:255]
        payment = self.client.create_payment({
            'Invoice': {'InvoiceID': info['id']},
            'Account': {'Code': account},
            'Date': paid_on,
            'Amount': round(amount, 2),
            'Reference': reference or f'Web order {shop.get("number") or ""}',
        })
        meta = xmeta(so)
        meta['payment_recorded'] = payment.get('PaymentID') or True
        meta['amount_paid'] = round(as_float(meta.get('amount_paid')) + amount, 2)
        meta['amount_due'] = round(due - amount, 2)
        write_meta(so, meta)
        self.log('info', 'export', 'so', 'payment', f'Recorded shop payment {amount:.2f} on {info["number"]}',
                 ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])

    # ---------------------------------------------------------------- sales planning
    def plan_sales_order(self, so):
        """Return (payload, note, doc_type)."""
        meta = xmeta(so)
        shop = self.shop_info(so)
        shop_invoice = shop and (self.setting('SHOP_ORDER_DOCUMENT') or 'invoice') == 'invoice'
        doc_type = meta.get('doc_type') or ('invoice' if shop_invoice else self.so_doc_type)
        name = self.status_name(self.so_status(), so.status)
        current = meta.get('status') or ''
        linked = bool(meta.get('id'))
        paid = as_float(meta.get('amount_paid')) > 0
        xero_only = (meta.get('xero_only') or {}).get('count')

        if current in FINAL_STATUSES[doc_type]:
            return None, f'Xero {doc_type} is {current}', doc_type

        if name in CANCELLED_NAMES:
            if not linked:
                return None, 'cancelled and not in Xero', doc_type
            if xero_only:
                return None, f'the Xero {doc_type} has lines added in Xero — cancel or credit it in Xero', doc_type
            if doc_type == 'invoice':
                if current in ('', 'DRAFT', 'SUBMITTED'):
                    self.safety.destructive('invoice', meta.get('number'), 'Delete')
                    return {'Status': 'DELETED'}, 'DELETED', doc_type
                if current == 'AUTHORISED' and not paid:
                    self.safety.destructive('invoice', meta.get('number'), 'Void')
                    return {'Status': 'VOIDED'}, 'VOIDED', doc_type
                return None, 'invoice has payments — credit it in Xero', doc_type
            return {'Status': 'DELETED' if current in ('', 'DRAFT') else 'DECLINED'}, 'cancel', doc_type

        if doc_type == 'invoice' and current == 'AUTHORISED' and paid:
            return None, 'invoice is part-paid in Xero — not updating lines', doc_type

        lines = self.so_lines(so)
        if not lines:
            return None, 'no line items', doc_type
        contact_id = self.contacts.ensure(so.customer)

        payload = {
            'Contact': {'ContactID': contact_id},
            'Date': self.order_date(so).isoformat(),
            'LineAmountTypes': self.setting('LINE_AMOUNT_TYPES') or 'Exclusive',
            'Reference': self.so_reference(so),
            'LineItems': lines,
        }
        self.currency_field(so, payload)

        if doc_type == 'invoice':
            authorise = current == 'AUTHORISED' or self.so_should_authorise(name)
            if shop and not shop['on_account'] and self.flag('SHOP_AUTHORISE_PAID'):
                authorise = True  # paid online: the invoice is final
            target = self.safety.cap('invoice', 'AUTHORISED' if authorise else 'DRAFT')
            if current == 'AUTHORISED':
                target = 'AUTHORISED'  # approved in Xero by a person: never moved back
            due = so.target_date or (self.order_date(so) + timedelta(days=self.number('SO_DUE_DAYS', 30)))
            if shop and not shop['on_account']:
                due = self.order_date(so)
            payload.update({'Type': 'ACCREC', 'DueDate': due.isoformat(), 'Status': target})
            if self.flag('SO_USE_INVENTREE_REFERENCE'):
                payload['InvoiceNumber'] = so.reference
        else:
            target = 'DRAFT' if (name == 'pending' and current in ('', 'DRAFT')) else 'ACCEPTED'
            payload['Status'] = target
            title = strip_tag(so.description)
            if title:
                payload['Title'] = title[:100]
            if so.target_date:
                payload['ExpiryDate'] = so.target_date.isoformat()
            if self.flag('SO_USE_INVENTREE_REFERENCE'):
                payload['QuoteNumber'] = so.reference
        return payload, target, doc_type

    def export_sales_order(self, so, force=False):
        meta = xmeta(so)
        payload, note, doc_type = self.plan_sales_order(so)
        if payload is None:
            meta.setdefault('doc_type', doc_type)
            return self._skip(so, 'so', meta, note, force)

        shop = self.shop_info(so)
        digest = payload_hash(payload)
        if meta.get('id') and digest == meta.get('hash') and not force:
            self.stats['unchanged'] += 1
            return 'unchanged'

        pre_status = None
        if doc_type == 'quote' and payload.get('Status') == 'ACCEPTED' and meta.get('status', '') in ('', 'DRAFT'):
            pre_status = 'SENT'  # Xero quotes must be SENT before they can be ACCEPTED

        doc, owned = self.guarded_send('so', doc_type, so, payload, meta, pre_status=pre_status)
        info = doc_info(doc_type, doc, self.short_code)
        action = 'update' if meta.get('id') else 'create'
        extra = {'hash': digest, 'line_ids': owned, 'origin': meta.get('origin', 'inventree')}
        if shop:
            extra['shop_order'] = shop.get('number') or shop.get('order_id')
        meta = self.save_link(so, info, meta, **extra)
        self.stats['exported'] += 1
        self.log('info', 'export', 'so', action,
                 f'{so.reference} → Xero {doc_type} {info["number"]} ({info["status"]})' + (' [web order]' if shop else ''),
                 ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])
        if shop and doc_type == 'invoice':
            self._invoice_shop_quote(so)
        if shop:
            try:
                self.record_shop_payment(so, info, shop, meta)
            except (DryRunWrite, WritesPaused):
                pass
            except XeroError as exc:
                self.log('error', 'export', 'so', 'payment', f'Could not record shop payment: {exc}',
                         ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])
        return f'{action}d {info["number"]} ({info["status"]})'

    def _invoice_shop_quote(self, so):
        """The Sales Order came from an accepted shop quote: mark that Xero quote Invoiced."""
        from .models import XeroQuoteLink

        link = XeroQuoteLink.objects.filter(Q(sales_order_id=so.pk) | Q(reference=so.customer_reference),
                                            final=False).exclude(xero_id='').first()
        if link is None:
            return
        if not link.sales_order_id:
            link.sales_order_id = so.pk
            link.save(update_fields=['sales_order_id'])
        if link.status != 'ACCEPTED' and self.shop_quotes.available():
            models = shop_models()
            quote = models[0].objects.filter(pk=link.quote_request_id).first() if models else None
            if quote is not None:
                self.shop_quotes.safe(self.shop_quotes.export_quote, quote)
                link.refresh_from_db()
        self.shop_quotes.mark_invoiced(link)

    def store_export_hash(self, order, kind):
        """After an import, record what an export would send so it is not pushed straight back."""
        try:
            if kind == 'po':
                payload, _ = self.plan_purchase_order(order)
            else:
                payload, _, _ = self.plan_sales_order(order)
        except (XeroError, Hold) as exc:
            logger.info('Xero: could not pre-compute export hash for %s: %s', order, exc)
            return
        if payload is not None:
            meta = xmeta(order)
            meta['hash'] = payload_hash(payload)
            write_meta(order, meta)

    # ================================================================== EXPORT: contacts
    def export_contacts(self):
        from company.models import Company

        for company in Company.objects.filter(**{f'metadata__{META_KEY}__has_key': 'contact_id'}):
            try:
                self.contacts.push_update(company)
            except DryRunWrite:
                self.stats['would_send'] += 1
            except WritesPaused:
                return
            except XeroError as exc:
                self.log('error', 'export', 'contact', 'error', f'{company.name}: {exc}', ref=company.name, pk=company.pk)

    # ================================================================== IMPORT: helpers
    def find_linked(self, model, info, extra_refs=()):
        order = model.objects.filter(**{f'metadata__{META_KEY}__id': info['id']}).first()
        if order:
            return order
        for ref in [info['number'], *extra_refs]:
            ref = (ref or '').strip()
            if not ref:
                continue
            candidate = model.objects.filter(reference=ref).first()
            if candidate and not xmeta(candidate).get('id'):
                return candidate
        return None

    def owned_candidates(self, lines, salable):
        """Xero lines InvenTree will hold: part lines (plus all real lines in 'extra' mode)."""
        result = []
        for ln in lines:
            qty, unit = to_decimal(ln.get('Quantity')), to_decimal(ln.get('UnitAmount'))
            if qty == 0 and unit == 0:
                continue  # heading / note line
            if self.items.has_part_match(ln.get('ItemCode') or '', salable=salable) or self.import_extra_lines:
                result.append(ln)
        return result

    @staticmethod
    def _line_numbers(ln):
        qty = to_decimal(ln.get('Quantity'))
        return (qty if qty != 0 else Decimal('1')), to_decimal(ln.get('UnitAmount')), to_decimal(ln.get('DiscountRate'))

    def _maybe_refresh_lines(self, order, info, meta, kind, create_func):
        """For Xero-originated orders still pending in InvenTree, mirror line changes made in Xero."""
        enum_cls = self.po_status() if kind == 'po' else self.so_status()
        if self.dry_run:
            return
        if not (self.flag('IMPORT_UPDATE_LINES') and meta.get('origin') == 'xero'
                and self.status_name(enum_cls, order.status) == 'pending'):
            return
        candidates = self.owned_candidates(info['lines'], salable=(kind == 'so'))
        sig = lines_signature(candidates)
        if sig == meta.get('lines_sig'):
            return
        with transaction.atomic():
            order.lines.all().delete()
            order.extra_lines.all().delete()
            meta['line_ids'] = create_func(order, info, candidates)
        meta['lines_sig'] = sig

    # ================================================================== IMPORT: purchase orders
    def import_purchase_orders(self):
        started = timezone.now()
        statuses = {s.upper() for s in self.csv('PO_IMPORT_STATUSES')}
        docs = self.client.get_all('PurchaseOrders', 'PurchaseOrders', modified_since=self.import_since('po'))
        for doc in docs:
            self.safe_import(self.import_purchase_order, doc_info('po', doc, self.short_code), statuses)
        self.set_import_mark('po', started)

    def import_purchase_order(self, info, statuses):
        from order.models import PurchaseOrder

        po = self.find_linked(PurchaseOrder, info)
        if po:
            return self.update_po_from_xero(info, po)
        if info['status'] not in statuses:
            self.stats['skipped'] += 1
            return 'skipped (status)'

        if not self.flag('IMPORT_NEW_DOCUMENTS'):
            self.stats['skipped'] += 1
            return 'skipped (importing new Xero documents is off)'
        if self.dry_run:
            self.stats['would_send'] += 1
            self.log('info', 'import', 'po', 'dry-run', f'Dry run - would import Xero PO {info["number"]}',
                     xero_id=info['id'], number=info['number'])
            return 'dry run'
        candidates = self.owned_candidates(info['lines'], salable=False)
        if not candidates and self.flag('IMPORT_SKIP_NO_PARTS'):
            self.stats['skipped'] += 1
            self.log('info', 'import', 'po', 'skip',
                     f'Xero PO {info["number"]} has no lines for parts held in InvenTree — left in Xero',
                     xero_id=info['id'], number=info['number'])
            return 'skipped (no parts)'

        supplier = self.contacts.company_for(info['contact_id'], info['contact_name'], 'supplier')
        if not supplier:
            self.stats['skipped'] += 1
            self.log('warning', 'import', 'po', 'skip',
                     f'No InvenTree supplier for Xero contact "{info["contact_name"]}" (company creation is off)',
                     xero_id=info['id'], number=info['number'])
            return 'skipped (no supplier)'

        with transaction.atomic():
            po = PurchaseOrder(
                reference=PurchaseOrder.generate_reference(),
                supplier=supplier,
                supplier_reference=(info['reference'] or info['number'])[:64],
                description=f'Imported from Xero {info["number"]}'[:250],
                target_date=info['due'].date() if info['due'] else None,
            )
            po.save()
            owned = self.create_po_lines(po, info, candidates)

        self.apply_po_status(po, info)
        self.save_link(po, info, {}, origin='xero', line_ids=owned, lines_sig=lines_signature(candidates))
        self.store_export_hash(po, 'po')
        self.stats['imported'] += 1
        kept = len([ln for ln in info['lines'] if ln.get('LineItemID') not in set(owned)])
        self.log('info', 'import', 'po', 'create',
                 f'Xero {info["number"]} → {po.reference} ({info["status"]})'
                 + (f'; {kept} Xero-only line(s) left in Xero' if kept else ''),
                 ref=po.reference, pk=po.pk, xero_id=info['id'], number=info['number'])
        return f'created {po.reference}'

    def create_po_lines(self, po, info, candidates):
        """Create InvenTree lines from the owned Xero lines; returns their LineItemIDs."""
        from djmoney.money import Money

        from common.currency import currency_code_default
        from order.models import PurchaseOrderExtraLine, PurchaseOrderLineItem

        currency = info['currency'] or currency_code_default()
        owned = []
        for ln in candidates:
            qty, unit, discount = self._line_numbers(ln)
            desc = ln.get('Description') or ''
            code = ln.get('ItemCode') or ''
            sp = self.items.match_supplier_part(code, po.supplier) if code else None
            if sp:
                PurchaseOrderLineItem.objects.create(
                    order=po, part=sp, quantity=qty, purchase_price=Money(unit, currency),
                    discount=discount, notes=desc[:500],
                )
            elif self.import_extra_lines:
                PurchaseOrderExtraLine.objects.create(
                    order=po, reference=code[:100], description=desc[:250] or code or 'Xero line',
                    quantity=qty, price=Money(unit, currency), discount=discount,
                )
            else:
                continue  # part exists but not from this supplier and supplier-part creation is off
            if ln.get('LineItemID'):
                owned.append(ln['LineItemID'])
        return owned

    def update_po_from_xero(self, info, po, force=False):
        meta = xmeta(po)
        stored = parse_xero_date(meta.get('updated'))
        if not force and stored and info['updated'] and info['updated'] <= stored:
            self.stats['unchanged'] += 1
            return 'unchanged'

        self._maybe_refresh_lines(po, info, meta, 'po', self.create_po_lines)
        previous = meta.get('status')
        self.apply_po_status(po, info)
        self.save_link(po, info, meta)
        self.store_export_hash(po, 'po')
        self.stats['updated'] += 1
        self.log('info', 'import', 'po', 'update',
                 f'Xero {info["number"]} {previous or "-"} → {info["status"]} applied to {po.reference}',
                 ref=po.reference, pk=po.pk, xero_id=info['id'], number=info['number'])
        return 'updated'

    def apply_po_status(self, po, info):
        if self.dry_run:
            return
        name = self.status_name(self.po_status(), po.status)
        status = info['status']
        if status in ('AUTHORISED', 'BILLED') and name in ('pending', 'on_hold'):
            self.transition(po.place_order)
        elif status == 'DELETED' and name in ('pending', 'placed', 'on_hold'):
            self.transition(po.cancel_order)
        po.refresh_from_db()

    # ================================================================== IMPORT: bills
    def import_bills(self):
        started = timezone.now()
        docs = self.client.get_all('Invoices', 'Invoices', params={'where': 'Type=="ACCPAY"'},
                                   modified_since=self.import_since('bill'))
        for doc in docs:
            self.safe_import(self.import_bill, doc_info('bill', doc, self.short_code))
        self.set_import_mark('bill', started)

    def import_bill(self, info, force=False):
        """Update a linked bill's status/payments, or link a bill made in Xero to its PO by reference."""
        from order.models import PurchaseOrder

        po = PurchaseOrder.objects.filter(**{f'metadata__{META_KEY}__bill__id': info['id']}).first()
        new_link = False
        if po is None and self.flag('LINK_XERO_BILLS'):
            tokens = {t for t in re.split(r'[\s/,;|()#]+', f'{info["reference"]} {info["number"]}') if t}
            for candidate in PurchaseOrder.objects.filter(reference__in=tokens):
                if not (xmeta(candidate).get('bill') or {}).get('id'):
                    po, new_link = candidate, True
                    break
        if po is None:
            return 'not linked'

        meta = xmeta(po)
        bill = meta.get('bill') or {}
        stored = parse_xero_date(bill.get('updated'))
        if not force and not new_link and stored and info['updated'] and info['updated'] <= stored:
            self.stats['unchanged'] += 1
            return 'unchanged'

        extra = {'origin': 'xero', 'line_ids': []} if new_link else {}
        meta['bill'] = meta_from_info(info, bill, **extra)
        write_meta(po, meta)
        self.stats['updated'] += 1
        self.log('info', 'import', 'bill', 'link' if new_link else 'update',
                 f'Xero bill {info["number"] or info["reference"]} ({info["status"]}, due {info["amount_due"]}) '
                 f'{"linked to" if new_link else "on"} {po.reference}',
                 ref=po.reference, pk=po.pk, xero_id=info['id'], number=info['number'])
        if info['status'] in ('AUTHORISED', 'PAID'):
            self.mark_po_billed(po)
        return 'updated'

    # ================================================================== IMPORT: sales
    def import_sales_orders(self):
        doc_type = self.so_doc_type
        started = timezone.now()
        since = self.import_since(doc_type)
        if doc_type == 'invoice':
            docs = self.client.get_all('Invoices', 'Invoices', params={'where': 'Type=="ACCREC"'}, modified_since=since)
            statuses = {s.upper() for s in self.csv('INVOICE_IMPORT_STATUSES')}
        else:
            docs = self.client.get_all('Quotes', 'Quotes', modified_since=since)
            statuses = {s.upper() for s in self.csv('QUOTE_IMPORT_STATUSES')}
        for doc in docs:
            self.safe_import(self.import_sales_doc, doc_info(doc_type, doc, self.short_code), statuses)
        self.set_import_mark(doc_type, started)
        # Invoices for web orders exported while in quote mode still need payment updates
        if doc_type == 'quote':
            self.safe_step(self._refresh_linked_invoices, 'Linked invoice refresh')

    def _refresh_linked_invoices(self):
        from order.models import SalesOrder

        started = timezone.now()
        if not SalesOrder.objects.filter(**{f'metadata__{META_KEY}__doc_type': 'invoice'}).exists():
            return
        docs = self.client.get_all('Invoices', 'Invoices', params={'where': 'Type=="ACCREC"'},
                                   modified_since=self.import_since('invoice'))
        for doc in docs:
            info = doc_info('invoice', doc, self.short_code)
            so = SalesOrder.objects.filter(**{f'metadata__{META_KEY}__id': info['id']}).first()
            if so:
                self.safe_import(self.update_so_from_xero, info, so)
        self.set_import_mark('invoice', started)

    def import_sales_doc(self, info, statuses):
        from order.models import SalesOrder

        if info['doc_type'] == 'quote':
            from .models import XeroQuoteLink

            if XeroQuoteLink.objects.filter(xero_id=info['id']).exists():
                return 'web shop quote (handled with the shop)'
        first_ref = (info['reference'] or '').split(' / ')[0]
        so = self.find_linked(SalesOrder, info, extra_refs=[first_ref])
        if so:
            return self.update_so_from_xero(info, so)
        if info['status'] not in statuses:
            self.stats['skipped'] += 1
            return 'skipped (status)'

        if not self.flag('IMPORT_NEW_DOCUMENTS'):
            self.stats['skipped'] += 1
            return 'skipped (importing new Xero documents is off)'
        if self.dry_run:
            self.stats['would_send'] += 1
            self.log('info', 'import', 'so', 'dry-run', f'Dry run - would import Xero {info["doc_type"]} {info["number"]}',
                     xero_id=info['id'], number=info['number'])
            return 'dry run'
        candidates = self.owned_candidates(info['lines'], salable=True)
        if not candidates and self.flag('IMPORT_SKIP_NO_PARTS'):
            self.stats['skipped'] += 1
            return 'skipped (no parts — labour, freight, interest etc. stay in Xero)'

        customer = self.contacts.company_for(info['contact_id'], info['contact_name'], 'customer')
        if not customer:
            self.stats['skipped'] += 1
            self.log('warning', 'import', 'so', 'skip',
                     f'No InvenTree customer for Xero contact "{info["contact_name"]}" (company creation is off)',
                     xero_id=info['id'], number=info['number'])
            return 'skipped (no customer)'

        with transaction.atomic():
            so = SalesOrder(
                reference=SalesOrder.generate_reference(),
                customer=customer,
                customer_reference=(info['reference'] or '')[:64],
                description=f'Imported from Xero {info["doc_type"]} {info["number"]}'[:250],
                target_date=info['due'].date() if info['due'] else None,
            )
            so.save()
            owned = self.create_so_lines(so, info, candidates)

        self.apply_so_status(so, info)
        self.save_link(so, info, {}, origin='xero', line_ids=owned, lines_sig=lines_signature(candidates))
        self.store_export_hash(so, 'so')
        self.stats['imported'] += 1
        kept = len([ln for ln in info['lines'] if ln.get('LineItemID') not in set(owned)])
        self.log('info', 'import', 'so', 'create',
                 f'Xero {info["number"]} → {so.reference} ({info["status"]})'
                 + (f'; {kept} Xero-only line(s) left in Xero' if kept else ''),
                 ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])
        return f'created {so.reference}'

    def create_so_lines(self, so, info, candidates):
        from djmoney.money import Money

        from common.currency import currency_code_default
        from order.models import SalesOrderExtraLine, SalesOrderLineItem

        currency = info['currency'] or currency_code_default()
        owned = []
        for ln in candidates:
            qty, unit, discount = self._line_numbers(ln)
            desc = ln.get('Description') or ''
            code = ln.get('ItemCode') or ''
            part = self.items.find_part(code) if code else None
            if part is not None and part.salable:
                SalesOrderLineItem.objects.create(
                    order=so, part=part, quantity=qty, sale_price=Money(unit, currency),
                    discount=discount, notes=desc[:500],
                )
            elif self.import_extra_lines:
                SalesOrderExtraLine.objects.create(
                    order=so, reference=code[:100], description=desc[:250] or code or 'Xero line',
                    quantity=qty, price=Money(unit, currency), discount=discount,
                )
            else:
                continue
            if ln.get('LineItemID'):
                owned.append(ln['LineItemID'])
        return owned

    def update_so_from_xero(self, info, so, force=False):
        meta = xmeta(so)
        stored = parse_xero_date(meta.get('updated'))
        if not force and stored and info['updated'] and info['updated'] <= stored:
            self.stats['unchanged'] += 1
            return 'unchanged'

        self._maybe_refresh_lines(so, info, meta, 'so', self.create_so_lines)
        previous = meta.get('status')
        self.apply_so_status(so, info)
        self.save_link(so, info, meta)
        self.store_export_hash(so, 'so')
        self.stats['updated'] += 1
        note = ''
        if info['doc_type'] == 'invoice' and info['status'] == 'PAID' and previous != 'PAID':
            note = ' — invoice PAID'
        self.log('info', 'import', 'so', 'update',
                 f'Xero {info["number"]} {previous or "-"} → {info["status"]} applied to {so.reference}{note}',
                 ref=so.reference, pk=so.pk, xero_id=info['id'], number=info['number'])
        return 'updated'

    def apply_so_status(self, so, info):
        if self.dry_run:
            return
        name = self.status_name(self.so_status(), so.status)
        doc_type = info['doc_type']
        status = info['status']
        if status in SO_CANCEL_FROM_XERO[doc_type] and name in ('pending', 'in_progress', 'on_hold'):
            self.transition(so.cancel_order)
        elif status in SO_ISSUE_FROM_XERO[doc_type] and name == 'pending' and self.flag('SO_ISSUE_ON_IMPORT'):
            self.transition(so.issue_order)
        so.refresh_from_db()

    # ================================================================== IMPORT: contact balances
    def import_balances(self):
        """Outstanding / overdue receivables and payables per linked company (for credit checks)."""
        from company.models import Company

        last = self.get_mark('balances')
        interval = max(self.number('BALANCE_INTERVAL_MINUTES', 60), 15)
        full = not last or last + timedelta(minutes=interval) <= timezone.now()
        started = timezone.now()
        companies = {}
        for company in Company.objects.filter(**{f'metadata__{META_KEY}__has_key': 'contact_id'}):
            meta = xmeta(company)
            if full or 'balance' not in meta:  # newly linked companies don't wait for the interval
                companies[meta['contact_id']] = company
        if not companies:
            return

        changed = []
        ids = list(companies)
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            rows = {c.get('ContactID'): c for c in self.client.get_contacts_by_id(chunk)}
            for contact_id in chunk:
                contact = rows.get(contact_id)
                if contact is None:
                    continue
                if 'Balances' not in contact:
                    contact = self.client.get_contact(contact_id)
                if 'Balances' not in contact:
                    continue
                if self._store_balance(companies[contact_id], contact.get('Balances') or {}):
                    changed.append(companies[contact_id].pk)

        if full:
            self.set_import_mark('balances', started)
        if changed:
            self.log('info', 'import', 'contact', 'balance', f'Updated Xero balances for {len(changed)} companies')
            try:
                from plugin.base.event.events import trigger_event

                trigger_event(EVENT_BALANCES, company_ids=changed)
            except Exception as exc:  # noqa: BLE001
                logger.info('Xero: could not fire %s: %s', EVENT_BALANCES, exc)

    def _store_balance(self, company, balances):
        receivable = balances.get('AccountsReceivable') or {}
        payable = balances.get('AccountsPayable') or {}
        values = {
            'receivable_outstanding': round(as_float(receivable.get('Outstanding')), 2),
            'receivable_overdue': round(as_float(receivable.get('Overdue')), 2),
            'payable_outstanding': round(as_float(payable.get('Outstanding')), 2),
            'payable_overdue': round(as_float(payable.get('Overdue')), 2),
        }
        meta = xmeta(company)
        old = meta.get('balance') or {}
        if all(old.get(k) == values[k] for k in BALANCE_FIELDS):
            return False
        meta['balance'] = {**values, 'updated': timezone.now().isoformat()}
        write_meta(company, meta)
        parameter = (self.setting('BALANCE_PARAMETER') or '').strip()
        if parameter and company.is_customer:
            self._write_parameter(company, parameter, values['receivable_outstanding'])
        return True

    @staticmethod
    def _write_parameter(company, name, value):
        from django.contrib.contenttypes.models import ContentType

        from common.models import Parameter, ParameterTemplate

        template = ParameterTemplate.objects.filter(name__iexact=name).first()
        if template is None:
            template = ParameterTemplate.objects.create(
                name=name, description='Outstanding balance in Xero (kept up to date by the Xero plugin)',
            )
        Parameter.objects.update_or_create(
            model_type=ContentType.objects.get_for_model(type(company)), model_id=company.pk, template=template,
            defaults={'data': f'{value:.2f}'},
        )


__all__ = ['XeroSync', 'EVENT_BALANCES', 'xero_only_summary']
