"""Live self-test against Xero's Demo Company.

Checks, against the real Xero API, every behaviour this plugin relies on that a
fake can't prove: document and status rules, line replacement and preserving
Xero-only lines, discounts and tax types, idempotency keys, If-Modified-Since,
payments, voiding, quotes (including Sent -> Draft for revisions), contacts,
balances and items.

It only runs when the connected organisation is a Xero Demo Company and you
confirm it. Everything it creates is tagged "INVENTREE-XERO-SELFTEST" and is
voided/deleted/archived afterwards where Xero allows (the Demo Company resets
itself every 28 days anyway). Writes made by the self-test are recorded in the
write audit like any other.

Run it from the plugin settings panel ("Run live self-test") or:
    python manage.py xero_selftest --confirm-demo
"""

from datetime import date, timedelta

from django.utils import timezone

from .checks import Report
from .mapping import deep_link, preserved_lines
from .xero_client import XeroClient, XeroError

TAG = 'INVENTREE-XERO-SELFTEST'
ABN_FOR_TESTS = '51 824 753 556'  # the ATO's own ABN - valid checksum, safe in a demo organisation


class Aborted(Exception):
    pass


class SelfTest:
    def __init__(self, plugin, client=None):
        self.plugin = plugin
        self.client = client or XeroClient(plugin)
        self.report = Report('inventree-xero LIVE self-test (Xero Demo Company)')
        self.stamp = timezone.now().strftime('%y%m%d%H%M%S')
        self.created = {'PurchaseOrders': [], 'Invoices': [], 'Quotes': []}
        self.contact_id = None
        self.item_id = None
        self.sales_account = None
        self.purchase_account = None
        self.bank_account = None
        self.sales_tax = None
        self.purchase_tax = None

    # ------------------------------------------------------------------ helpers
    def post(self, resource, doc):
        return self.client.post_document(resource, resource, doc)

    def step(self, name, func, expect_refusal=False):
        """Run one check. func returns a message (PASS) or raises XeroError."""
        try:
            message = func()
        except AssertionError as exc:
            self.report.fail(name, str(exc))
            return False
        except XeroError as exc:
            if expect_refusal:
                self.report.ok(name, f'Xero refused it, as the plugin expects ({exc})')
                return True
            self.report.fail(name, str(exc))
            return False
        if expect_refusal:
            self.report.warn(name, f'Xero allowed it, but the plugin assumes it would be refused: {message}')
            return False
        self.report.ok(name, message)
        return True

    def line(self, description, quantity, unit, account, tax=None, discount=None, code=None):
        out = {'Description': description, 'Quantity': quantity, 'UnitAmount': unit, 'AccountCode': account}
        if tax:
            out['TaxType'] = tax
        if discount:
            out['DiscountRate'] = discount
        if code:
            out['ItemCode'] = code
        return out

    # ------------------------------------------------------------------ run
    def run(self, confirm_demo=False):
        try:
            self._guard(confirm_demo)
            self._accounts()
            self._contacts()
            self._items()
            self._purchase_orders()
            self._invoices()
            self._bills()
            self._quotes()
            self._links()
        except Aborted as exc:
            self.report.fail('Self-test stopped', str(exc))
        except XeroError as exc:
            self.report.fail('Self-test stopped', f'Unexpected Xero error: {exc}')
        finally:
            self._cleanup()
            self.client.force_live = False
            self.client.audit_context = {}
        return self.report

    def _guard(self, confirm_demo):
        if self.client.paused:
            raise Aborted('Writes are paused (kill switch) - switch to Dry run or Live to run the self-test')
        if not self.client.conn.is_connected:
            raise Aborted('Not connected to Xero')
        self.client.ensure_token()
        org = self.client.load_organisation()
        demo = bool(org.get('IsDemoCompany')) or org.get('Class') == 'DEMO'
        if not demo:
            raise Aborted(f'"{org.get("Name")}" is not a Xero Demo Company. The self-test writes test documents, so it '
                          f'only runs against a Demo Company - connect one (Xero > your name > Demo Company) first')
        if not confirm_demo:
            raise Aborted('Confirm that you want to write test documents to the Demo Company')
        self.report.ok('Organisation', f'"{org.get("Name")}" is a Demo Company ({org.get("CountryCode")}, '
                                       f'{org.get("BaseCurrency")}) - test documents are tagged {TAG}-{self.stamp}')
        self.client.force_live = True  # the self-test writes even when the plugin is in Dry run
        self.client.audit_context = {'doc_type': 'selftest', 'ref': f'SELFTEST-{self.stamp}'}

    def _accounts(self):
        setting = self.plugin.get_setting
        accounts = {a['Code']: a for a in self.client.get('Accounts').get('Accounts') or []
                    if a.get('Code') and a.get('Status', 'ACTIVE') == 'ACTIVE'}
        rates = {t['TaxType']: t for t in self.client.get('TaxRates').get('TaxRates') or [] if t.get('TaxType')}

        def pick(code, types):
            if code and code in accounts:
                return code
            return next((c for c, a in accounts.items() if a.get('Type') in types), None)

        self.sales_account = pick((setting('SALES_ACCOUNT_CODE') or '').strip(), {'REVENUE', 'SALES'})
        self.purchase_account = pick((setting('PURCHASE_ACCOUNT_CODE') or '').strip(),
                                     {'EXPENSE', 'DIRECTCOSTS', 'OVERHEADS'})
        self.bank_account = next((c for c, a in accounts.items() if a.get('Type') == 'BANK'), None)
        self.sales_tax = (setting('SALES_TAX_TYPE') or '').strip() or None
        self.purchase_tax = (setting('PURCHASE_TAX_TYPE') or '').strip() or None
        if self.sales_tax and self.sales_tax not in rates:
            self.sales_tax = None
        if self.purchase_tax and self.purchase_tax not in rates:
            self.purchase_tax = None
        if not self.sales_account or not self.purchase_account:
            raise Aborted('Could not find a sales and a purchase account in the Demo Company')
        self.report.ok('Chart of accounts', f'Using sales {self.sales_account}, purchases {self.purchase_account}, '
                                            f'tax {self.sales_tax or "account default"} / '
                                            f'{self.purchase_tax or "account default"}')

    # ------------------------------------------------------------------ contacts
    def _contacts(self):
        name = f'InvenTree self-test {self.stamp}'

        def create():
            contact = self.post('Contacts', {'Name': name, 'EmailAddress': f'selftest-{self.stamp}@example.com',
                                             'TaxNumber': ABN_FOR_TESTS,
                                             'Phones': [{'PhoneType': 'DEFAULT', 'PhoneNumber': '08 9000 0000'}]})
            self.contact_id = contact['ContactID']
            assert contact.get('TaxNumber'), 'Xero did not keep the tax number (ABN)'
            return f'created "{name}" with email, phone and ABN'

        if not self.step('Contact: create', create):
            raise Aborted('Cannot continue without a test contact')

        def search():
            found = [c for c in self.client.search_contacts(name) if c.get('ContactID') == self.contact_id]
            assert found, 'searchTerm did not find the new contact (the plugin matches contacts this way)'
            fields = [k for k in ('EmailAddress', 'TaxNumber') if k in found[0]]
            return f'found by searchTerm; list rows include {", ".join(fields) or "no email/ABN"} for strict matching'

        self.step('Contact: search by name', search)

        def update():
            self.post('Contacts', {'ContactID': self.contact_id, 'Name': name,
                                   'Phones': [{'PhoneType': 'DEFAULT', 'PhoneNumber': '08 9000 1111'}]})
            phones = self.client.get_contact(self.contact_id).get('Phones') or []
            assert any(p.get('PhoneNumber') == '08 9000 1111' for p in phones), 'phone change not saved'
            return 'detail update saved'

        self.step('Contact: update details', update)

        rows = self.client.get_contacts_by_id([self.contact_id])
        single = self.client.get_contact(self.contact_id)
        self.report.info('Contact: balances', f'list by IDs {"includes" if rows and "Balances" in rows[0] else "does NOT include"} '
                                              f'Balances; single contact {"includes" if "Balances" in single else "does NOT include"} '
                                              f'Balances (the plugin handles both)')

    # ------------------------------------------------------------------ items
    def _items(self):
        code = f'IXT{self.stamp}'[:30]

        def create():
            item = self.post('Items', {'Code': code, 'Name': f'{TAG} item', 'IsTrackedAsInventory': False,
                                       'PurchaseDetails': {'AccountCode': self.purchase_account},
                                       'SalesDetails': {'AccountCode': self.sales_account}})
            self.item_id = item.get('ItemID')
            assert any(i.get('Code') == code for i in self.client.get_items()), 'new item not listed'
            return f'item {code} created and listed'

        self.step('Items: create and list', create)
        self.item_code = code if self.item_id else None

    # ------------------------------------------------------------------ purchase orders
    def _purchase_orders(self):
        number = f'IXT-PO-{self.stamp}'
        doc = {
            'Contact': {'ContactID': self.contact_id}, 'Date': date.today().isoformat(),
            'DeliveryDate': (date.today() + timedelta(days=14)).isoformat(), 'PurchaseOrderNumber': number,
            'Reference': f'{TAG}-{self.stamp}', 'LineAmountTypes': 'Exclusive', 'Status': 'DRAFT',
            'LineItems': [
                self.line('Plug (discounted)', 10, 2.5, self.purchase_account, self.purchase_tax, 10, self.item_code),
                self.line('Housing', 4, 1.25, self.purchase_account, self.purchase_tax),
            ],
        }
        po = {}

        def create():
            po.update(self.post('PurchaseOrders', doc))
            self.created['PurchaseOrders'].append(po['PurchaseOrderID'])
            assert po.get('Status') == 'DRAFT', f'expected DRAFT, got {po.get("Status")}'
            subtotal = float(po.get('SubTotal') or 0)
            assert abs(subtotal - 27.5) < 0.011, f'subtotal {subtotal} - expected 27.50 (10 x 2.50 less 10% + 4 x 1.25)'
            assert any(float(ln.get('DiscountRate') or 0) == 10 for ln in po.get('LineItems') or []), 'discount dropped'
            return f'{po.get("PurchaseOrderNumber")} created as Draft; discount kept; subtotal 27.50 as InvenTree adds it up'

        if not self.step('Purchase order: create Draft', create):
            return

        def idempotent():
            again = self.post('PurchaseOrders', doc)  # identical request -> identical Idempotency-Key
            if again.get('PurchaseOrderID') != po['PurchaseOrderID']:
                self.created['PurchaseOrders'].append(again.get('PurchaseOrderID'))
                raise AssertionError('Xero created a SECOND purchase order for the same request - idempotency keys '
                                     'are not being honoured')
            return 'repeating the same request returned the same purchase order (no duplicate)'

        self.step('Idempotency key: no duplicates on retry', idempotent)

        def preserve():
            current = self.client.get_document('po', po['PurchaseOrderID'])
            owned = [ln['LineItemID'] for ln in current['LineItems']]
            # someone adds freight in Xero
            self.post('PurchaseOrders', {'PurchaseOrderID': po['PurchaseOrderID'], 'LineItems': current['LineItems'] + [
                self.line('Freight (added in Xero)', 1, 15, self.purchase_account, self.purchase_tax)]})
            current = self.client.get_document('po', po['PurchaseOrderID'])
            kept = preserved_lines(current['LineItems'], owned)
            assert len(kept) == 1, f'expected 1 Xero-only line, found {len(kept)}'
            # InvenTree changes its own lines, sending the Xero-only line back untouched
            ours = [dict(doc['LineItems'][0], Quantity=12), doc['LineItems'][1]]
            updated = self.post('PurchaseOrders', {'PurchaseOrderID': po['PurchaseOrderID'], 'LineItems': ours + kept})
            descriptions = [ln.get('Description') for ln in updated.get('LineItems') or []]
            assert 'Freight (added in Xero)' in descriptions, 'the Xero-only line was lost'
            assert any(float(ln.get('Quantity') or 0) == 12 for ln in updated['LineItems']), 'InvenTree change not applied'
            return 'line set replaced; the Xero-only freight line survived the update'

        self.step('Purchase order: keep Xero-only lines', preserve)

        def modified_since():
            since = timezone.now() - timedelta(minutes=15)
            rows = self.client.get_all('PurchaseOrders', 'PurchaseOrders', modified_since=since)
            assert any(r.get('PurchaseOrderID') == po['PurchaseOrderID'] for r in rows), \
                'If-Modified-Since did not return the PO just changed'
            return f'If-Modified-Since returned it ({len(rows)} changed in 15 min)'

        self.step('Incremental import (If-Modified-Since)', modified_since)

        self.step('Purchase order: approve', lambda: self._status('PurchaseOrders', 'PurchaseOrderID', po, 'AUTHORISED'))
        self.step('Purchase order: cannot go back to Draft', lambda: self._status(
            'PurchaseOrders', 'PurchaseOrderID', po, 'DRAFT'), expect_refusal=True)
        self.step('Purchase order: mark Billed', lambda: self._status('PurchaseOrders', 'PurchaseOrderID', po, 'BILLED'))

        def delete_draft():
            spare = self.post('PurchaseOrders', dict(doc, PurchaseOrderNumber=f'{number}-2', Reference=f'{TAG}-{self.stamp}-2'))
            self.created['PurchaseOrders'].append(spare['PurchaseOrderID'])
            gone = self.post('PurchaseOrders', {'PurchaseOrderID': spare['PurchaseOrderID'], 'Status': 'DELETED'})
            assert gone.get('Status') == 'DELETED', f'status {gone.get("Status")}'
            return 'a draft purchase order can be deleted'

        self.step('Purchase order: delete a draft', delete_draft)

    def _status(self, resource, id_key, doc, status):
        result = self.post(resource, {id_key: doc[id_key], 'Status': status})
        assert result.get('Status') == status, f'status is {result.get("Status")}, expected {status}'
        doc.update(result)
        return f'now {status}'

    # ------------------------------------------------------------------ invoices
    def _invoices(self):
        base = {
            'Type': 'ACCREC', 'Contact': {'ContactID': self.contact_id}, 'Date': date.today().isoformat(),
            'DueDate': (date.today() + timedelta(days=30)).isoformat(), 'LineAmountTypes': 'Exclusive',
            'Reference': f'{TAG}-{self.stamp}',
            'LineItems': [self.line('Engine harness', 1, 480, self.sales_account, self.sales_tax, 5)],
        }
        inv = {}

        def create():
            inv.update(self.post('Invoices', dict(base, Status='DRAFT')))
            self.created['Invoices'].append(inv['InvoiceID'])
            assert inv.get('Status') == 'DRAFT', inv.get('Status')
            assert abs(float(inv.get('SubTotal') or 0) - 456.0) < 0.011, f'subtotal {inv.get("SubTotal")} - expected 456.00'
            return f'{inv.get("InvoiceNumber")} created as Draft; 5% discount; tax {self.sales_tax or "account default"}'

        if not self.step('Invoice: create Draft', create):
            return
        self.step('Invoice: approve', lambda: self._status('Invoices', 'InvoiceID', inv, 'AUTHORISED'))

        granted = set((self.client.conn.scopes or '').split())
        if 'accounting.payments' not in granted and 'accounting.transactions' not in granted:
            self.report.info('Invoice: record payment', 'Skipped - the connection has no payments permission (only '
                                                        'needed if the plugin records web shop payments)')
        elif not self.bank_account:
            self.report.info('Invoice: record payment', 'Skipped - no bank account in the Demo Company')
        else:
            def pay():
                self.client.create_payment({'Invoice': {'InvoiceID': inv['InvoiceID']}, 'Account': {'Code': self.bank_account},
                                            'Date': date.today().isoformat(), 'Amount': float(inv.get('AmountDue') or 0),
                                            'Reference': f'{TAG} payment'})
                paid = self.client.get_document('invoice', inv['InvoiceID'])
                assert paid.get('Status') == 'PAID' and float(paid.get('AmountDue') or 0) == 0, \
                    f'status {paid.get("Status")}, due {paid.get("AmountDue")}'
                return 'payment applied; invoice PAID'

            self.step('Invoice: record payment', pay)

        def void():
            other = self.post('Invoices', dict(base, Status='AUTHORISED', Reference=f'{TAG}-{self.stamp}-void'))
            self.created['Invoices'].append(other['InvoiceID'])
            voided = self.post('Invoices', {'InvoiceID': other['InvoiceID'], 'Status': 'VOIDED'})
            assert voided.get('Status') == 'VOIDED', voided.get('Status')
            return 'an unpaid approved invoice can be voided'

        self.step('Invoice: void', void)

        def delete():
            draft = self.post('Invoices', dict(base, Status='DRAFT', Reference=f'{TAG}-{self.stamp}-del'))
            self.created['Invoices'].append(draft['InvoiceID'])
            gone = self.post('Invoices', {'InvoiceID': draft['InvoiceID'], 'Status': 'DELETED'})
            assert gone.get('Status') == 'DELETED', gone.get('Status')
            return 'a draft invoice can be deleted'

        self.step('Invoice: delete a draft', delete)

    # ------------------------------------------------------------------ bills
    def _bills(self):
        bill = {}

        def create():
            bill.update(self.post('Invoices', {
                'Type': 'ACCPAY', 'Contact': {'ContactID': self.contact_id}, 'Date': date.today().isoformat(),
                'DueDate': (date.today() + timedelta(days=30)).isoformat(), 'LineAmountTypes': 'Exclusive',
                'Reference': f'IXT-PO-{self.stamp} / {TAG}', 'Status': 'DRAFT',
                'LineItems': [self.line('Plug received', 12, 2.5, self.purchase_account, self.purchase_tax)],
            }))
            self.created['Invoices'].append(bill['InvoiceID'])
            assert bill.get('Type') == 'ACCPAY' and bill.get('Status') == 'DRAFT'
            return 'draft bill created with the PO number in its reference'

        if not self.step('Bill: create Draft', create):
            return

        def preserve():
            owned = [ln['LineItemID'] for ln in bill['LineItems']]
            self.post('Invoices', {'InvoiceID': bill['InvoiceID'], 'LineItems': bill['LineItems'] + [
                self.line('Supplier freight', 1, 18, self.purchase_account, self.purchase_tax)]})
            current = self.client.get_document('bill', bill['InvoiceID'])
            kept = preserved_lines(current['LineItems'], owned)
            updated = self.post('Invoices', {'InvoiceID': bill['InvoiceID'], 'LineItems': [
                self.line('Plug received', 20, 2.5, self.purchase_account, self.purchase_tax)] + kept})
            assert any(ln.get('Description') == 'Supplier freight' for ln in updated['LineItems']), 'freight lost'
            return 'received quantity updated; supplier freight added in Xero kept'

        self.step('Bill: follow receipts, keep Xero-only lines', preserve)
        self.step('Bill: approve', lambda: self._status('Invoices', 'InvoiceID', bill, 'AUTHORISED'))

    # ------------------------------------------------------------------ quotes
    def _quotes(self):
        base = {
            'Contact': {'ContactID': self.contact_id}, 'Date': date.today().isoformat(),
            'ExpiryDate': (date.today() + timedelta(days=30)).isoformat(), 'Reference': f'QR-{self.stamp}',
            'Title': f'Quote request QR-{self.stamp}', 'LineAmountTypes': 'Exclusive',
            'LineItems': [self.line('Item 1: Engine harness copy (lead time 6 weeks)', 2, 1450, self.sales_account,
                                    self.sales_tax)],
        }
        quote = {}

        def create():
            quote.update(self.post('Quotes', dict(base, Status='DRAFT')))
            self.created['Quotes'].append(quote['QuoteID'])
            assert quote.get('Status') == 'DRAFT', quote.get('Status')
            return f'{quote.get("QuoteNumber")} created as Draft with reference, title and expiry'

        if not self.step('Quote: create Draft', create):
            return
        self.step('Quote: mark Sent', lambda: self._status('Quotes', 'QuoteID', quote, 'SENT'))

        def back_to_draft():
            result = self._status('Quotes', 'QuoteID', quote, 'DRAFT')
            return result + ' - revised quotes can go back to Draft, as the plugin expects'

        if not self.step('Quote: Sent back to Draft (revision)', back_to_draft):
            self.report.warn('Quote: revisions', 'Xero would not move a sent quote back to Draft. Turn on "Re-send '
                                                 'revised quotes automatically", or revise quotes in the shop widget')
            quote['Status'] = 'SENT'
        if quote.get('Status') != 'SENT':
            self.step('Quote: Sent again', lambda: self._status('Quotes', 'QuoteID', quote, 'SENT'))
        self.step('Quote: accept', lambda: self._status('Quotes', 'QuoteID', quote, 'ACCEPTED'))
        self.step('Quote: mark Invoiced', lambda: self._status('Quotes', 'QuoteID', quote, 'INVOICED'))

        def decline():
            other = self.post('Quotes', dict(base, Status='SENT', Reference=f'QR-{self.stamp}-2',
                                             Summary='Self-test decline reason'))
            self.created['Quotes'].append(other['QuoteID'])
            declined = self.post('Quotes', {'QuoteID': other['QuoteID'], 'Contact': base['Contact'], 'Date': base['Date'],
                                            'Status': 'DECLINED'})
            assert declined.get('Status') == 'DECLINED', declined.get('Status')
            assert declined.get('Summary') == 'Self-test decline reason', 'the Summary (decline reason) was not kept'
            return 'a sent quote can be declined; its Summary (the reason the shop sends) is kept'

        self.step('Quote: decline with a reason', decline)

    # ------------------------------------------------------------------ links
    def _links(self):
        short = self.client.conn.short_code
        urls = []
        if self.created['PurchaseOrders']:
            urls.append(('purchase order', deep_link('po', self.created['PurchaseOrders'][0], short)))
        if self.created['Invoices']:
            urls.append(('invoice', deep_link('invoice', self.created['Invoices'][0], short)))
        if self.created['Quotes']:
            urls.append(('quote', deep_link('quote', self.created['Quotes'][0], short)))
        if self.contact_id:
            urls.append(('contact', deep_link('contact', self.contact_id, short)))
        self.report.info('"Open in Xero" links', 'Open these while signed in to the Demo Company and check each lands on '
                                                 'the right page: ' + ' | '.join(f'{label}: {url}' for label, url in urls))

    # ------------------------------------------------------------------ cleanup
    def _cleanup(self):
        notes = []
        if self.contact_id and self.client.force_live:
            try:
                self.post('Contacts', {'ContactID': self.contact_id, 'ContactStatus': 'ARCHIVED'})
                notes.append('test contact archived')
            except XeroError as exc:
                notes.append(f'test contact not archived ({exc})')
        if self.item_id and self.client.force_live:
            try:
                self.client.request('DELETE', f'Items/{self.item_id}')
                notes.append('test item deleted')
            except XeroError as exc:
                notes.append(f'test item kept ({exc})')
        if notes or any(self.created.values()):
            notes.append('test documents left voided/approved/billed are tagged and vanish when the Demo Company resets')
            self.report.info('Clean-up', '; '.join(notes))


def run_selftest(plugin, confirm_demo=False, client=None):
    from .models import XeroSyncLog

    report = SelfTest(plugin, client=client).run(confirm_demo=confirm_demo)
    XeroSyncLog.objects.create(level='info' if report.passed else 'error', direction='system', doc_type='system',
                               action='selftest', message=report.as_text())
    return report


def run_check(plugin, client=None):
    from .checks import check_configuration
    from .models import XeroSyncLog

    report = check_configuration(plugin, client=client)
    XeroSyncLog.objects.create(level='info' if report.passed else 'warning', direction='system', doc_type='system',
                               action='check', message=report.as_text())
    return report
