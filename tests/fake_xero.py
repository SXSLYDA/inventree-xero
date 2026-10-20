"""In-memory stand-in for the Xero Accounting API, used by the end-to-end test.

Behaves like Xero where the plugin depends on it: assigns IDs, numbers and
LineItemIDs, replaces the line set on update, refuses to move a PO backwards,
applies payments, filters by If-Modified-Since and paginates at 100.
"""

import copy
import itertools
import uuid
from datetime import datetime, timezone

import requests

from inventree_xero.xero_client import API_BASE, XeroClient, XeroError

PO_RANK = {'DRAFT': 0, 'SUBMITTED': 1, 'AUTHORISED': 2, 'BILLED': 3, 'DELETED': 9}


def xero_now():
    return f'/Date({int(datetime.now(timezone.utc).timestamp() * 1000)}+0000)/'


def parse_since(dt):
    return dt.timestamp() * 1000 if dt else None


def ms(value):
    return int(value[6:value.index('+')])


class FakeXeroClient(XeroClient):
    """XeroClient whose HTTP layer is an in-memory Xero organisation."""

    counter = itertools.count(1)

    def __init__(self, plugin):
        super().__init__(plugin)
        self.store = {'PurchaseOrders': {}, 'Invoices': {}, 'Quotes': {}, 'Contacts': {}, 'Items': {}, 'Payments': {}}
        self.calls = []
        self.balances = {}
        self.idempotent = {}        # Idempotency-Key -> response, like Xero
        self.drop_after_commit = 0  # simulate N lost responses (Xero saved it, the reply never arrived)
        self.headers_seen = []
        self.org = {'Name': 'Demo Company (AU)', 'ShortCode': '!demo', 'BaseCurrency': 'AUD', 'CountryCode': 'AU',
                    'IsDemoCompany': True, 'Class': 'DEMO', 'PeriodLockDate': '/Date(1719705600000+0000)/'}
        self.accounts = [
            {'Code': '090', 'Name': 'Business Bank Account', 'Type': 'BANK', 'Status': 'ACTIVE'},
            {'Code': '200', 'Name': 'Sales', 'Type': 'REVENUE', 'Status': 'ACTIVE'},
            {'Code': '300', 'Name': 'Purchases', 'Type': 'DIRECTCOSTS', 'Status': 'ACTIVE'},
            {'Code': '425', 'Name': 'Freight & Courier', 'Type': 'EXPENSE', 'Status': 'ACTIVE'},
        ]
        self.tax_rates = [
            {'TaxType': 'INPUT', 'Name': 'GST on Expenses', 'Status': 'ACTIVE', 'CanApplyToExpenses': True,
             'CanApplyToRevenue': False, 'EffectiveRate': 10},
            {'TaxType': 'OUTPUT', 'Name': 'GST on Income', 'Status': 'ACTIVE', 'CanApplyToExpenses': False,
             'CanApplyToRevenue': True, 'EffectiveRate': 10},
        ]

    # --- auth is not exercised here
    def ensure_token(self):
        return None

    # --- helpers
    @staticmethod
    def _id():
        return str(uuid.uuid4())

    def _lines(self, lines):
        out = []
        for line in lines or []:
            line = dict(line)
            line.setdefault('LineItemID', self._id())
            qty = float(line.get('Quantity') or 0)
            unit = float(line.get('UnitAmount') or 0)
            disc = float(line.get('DiscountRate') or 0)
            line['LineAmount'] = round(qty * unit * (1 - disc / 100), 2)
            out.append(line)
        return out

    def _finish(self, doc):
        doc['LineItems'] = self._lines(doc.get('LineItems'))
        amounts = doc.get('LineAmountTypes') or 'Exclusive'
        for line in doc['LineItems']:
            rate = 0 if amounts == 'NoTax' or line.get('TaxType') in ('EXEMPTOUTPUT', 'BASEEXCLUDED') else 10
            amount = line['LineAmount']
            line['TaxAmount'] = round(amount * rate / (100 + rate) if amounts == 'Inclusive' else amount * rate / 100, 2)
        total = round(sum(ln['LineAmount'] for ln in doc['LineItems']), 2)
        doc['SubTotal'] = doc['Total'] = total
        paid = float(doc.get('AmountPaid') or 0)
        doc['AmountPaid'] = paid
        doc['AmountDue'] = round(total - paid, 2)
        doc['UpdatedDateUTC'] = xero_now()
        contact_id = (doc.get('Contact') or {}).get('ContactID')
        if contact_id in self.store['Contacts']:
            doc['Contact'] = {'ContactID': contact_id, 'Name': self.store['Contacts'][contact_id]['Name']}
        return doc

    # --- test helpers
    def add_contact(self, name, balances=None, **fields):
        contact = {'ContactID': self._id(), 'Name': name, 'ContactStatus': 'ACTIVE', **fields}
        if balances:
            self.balances[contact['ContactID']] = balances
        self.store['Contacts'][contact['ContactID']] = contact
        return contact

    def add_document(self, resource, doc):
        id_key = {'PurchaseOrders': 'PurchaseOrderID', 'Invoices': 'InvoiceID', 'Quotes': 'QuoteID'}[resource]
        doc = dict(doc)
        doc[id_key] = self._id()
        self.store[resource][doc[id_key]] = self._finish(doc)
        return doc

    def edit_in_xero(self, resource, doc_id, **changes):
        """Simulate someone editing a document in Xero."""
        doc = self.store[resource][doc_id]
        doc.update(changes)
        self._finish(doc)
        return doc

    # --- HTTP layer: everything above this (dry run, audit, idempotency, retries) is the real client
    def _http(self, method, url, params=None, json_body=None, headers=None):
        headers = headers or {}
        self.headers_seen.append((method, url, dict(headers)))
        path = url[len(API_BASE) + 1:] if url.startswith(API_BASE) else url
        key = headers.get('Idempotency-Key')
        if key and key in self.idempotent:
            return 200, {}, copy.deepcopy(self.idempotent[key])
        modified_since = None
        if headers.get('If-Modified-Since'):
            modified_since = datetime.fromisoformat(headers['If-Modified-Since']).replace(tzinfo=timezone.utc)
        try:
            payload = self.route(method, path, params, copy.deepcopy(json_body), modified_since)
        except XeroError as exc:
            return exc.status or 400, {}, {'Message': str(exc)}
        if key:
            self.idempotent[key] = copy.deepcopy(payload)
        if method != 'GET' and self.drop_after_commit:
            self.drop_after_commit -= 1
            raise requests.ConnectionError('connection reset after the request was processed')
        return 200, {}, payload

    def _http_raw(self, method, url, headers=None):
        """PDFs: GET Quotes/{id} (or any document) with Accept: application/pdf."""
        headers = headers or {}
        self.headers_seen.append((method, url, dict(headers)))
        parts = url[len(API_BASE) + 1:].split('/')
        doc = self.store.get(parts[0], {}).get(parts[1]) if len(parts) == 2 else None
        if method != 'GET' or headers.get('Accept') != 'application/pdf' or doc is None:
            return 404, {}, b'{"Message": "not found"}'
        number = doc.get('QuoteNumber') or doc.get('InvoiceNumber') or doc.get('PurchaseOrderNumber') or ''
        return 200, {'Content-Type': 'application/pdf'}, f'%PDF-1.4 fake Xero {number} {doc.get("Total")}'.encode()

    def time_sleep(self, *_):
        return None

    # --- the API
    def route(self, method, path, params=None, json_body=None, modified_since=None):
        self.calls.append((method, path))
        params = params or {}
        parts = path.strip('/').split('/')
        resource = parts[0]

        if resource == 'Organisation':
            return {'Organisations': [dict(self.org)]}
        if resource == 'Accounts':
            return {'Accounts': [dict(a) for a in self.accounts]}
        if resource == 'TaxRates':
            return {'TaxRates': [dict(t) for t in self.tax_rates]}
        if method == 'DELETE' and resource == 'Items':
            self.store['Items'].pop(parts[1], None)
            return {}

        if resource == 'Payments' and method == 'PUT':
            payment = dict(json_body['Payments'][0])
            invoice = self.store['Invoices'][payment['Invoice']['InvoiceID']]
            if invoice['Status'] != 'AUTHORISED':
                raise XeroError('Payment can only be applied to an approved invoice', 400)
            invoice['AmountPaid'] = round(float(invoice.get('AmountPaid') or 0) + payment['Amount'], 2)
            self._finish(invoice)
            if invoice['AmountDue'] <= 0:
                invoice['Status'] = 'PAID'
            payment['PaymentID'] = self._id()
            self.store['Payments'][payment['PaymentID']] = payment
            return {'Payments': [payment]}

        store = self.store[resource]

        if method == 'GET':
            if len(parts) > 1:
                row = store.get(parts[1])
                if row is None:
                    raise XeroError('Not found', 404)
                return {resource: [self._with_balances(resource, row)]}
            rows = list(store.values())
            since = parse_since(modified_since)
            if since:
                since -= 1000  # header has second precision
            if since and resource != 'Items':
                rows = [r for r in rows if ms(r['UpdatedDateUTC']) >= since]
            where = params.get('where', '')
            if 'Type=="ACCREC"' in where:
                rows = [r for r in rows if r.get('Type') == 'ACCREC']
            if 'Type=="ACCPAY"' in where:
                rows = [r for r in rows if r.get('Type') == 'ACCPAY']
            if params.get('searchTerm'):
                term = params['searchTerm'].lower()
                rows = [r for r in rows if term in r.get('Name', '').lower()]
            if params.get('IDs'):
                ids = params['IDs'].split(',')
                rows = [r for r in rows if r['ContactID'] in ids]  # list results carry no Balances
            if resource == 'Items':
                return {resource: rows}
            page = int(params.get('page', 1))
            return {resource: rows[(page - 1) * 100: page * 100]}

        if method == 'POST':
            key = resource
            doc = dict(json_body[key][0])
            id_key = {'PurchaseOrders': 'PurchaseOrderID', 'Invoices': 'InvoiceID', 'Quotes': 'QuoteID',
                      'Contacts': 'ContactID', 'Items': 'ItemID'}[resource]
            existing = store.get(doc.get(id_key))
            if resource in ('Contacts', 'Items'):
                if existing:
                    existing.update(doc)
                    return {key: [existing]}
                doc[id_key] = self._id()
                store[doc[id_key]] = doc
                return {key: [doc]}
            if resource == 'Quotes' and doc.get('Status') in ('ACCEPTED', 'DECLINED', 'INVOICED'):
                previous = existing['Status'] if existing else None
                allowed = {'ACCEPTED': ('SENT', 'ACCEPTED'), 'DECLINED': ('SENT', 'DECLINED'), 'INVOICED': ('ACCEPTED',)}
                if previous not in allowed[doc['Status']]:
                    raise XeroError(f'A quote cannot go from {previous or "new"} to {doc["Status"]}', 400)
            if existing:
                if resource == 'PurchaseOrders' and PO_RANK.get(doc.get('Status', existing['Status']), 0) < PO_RANK[existing['Status']]:
                    raise XeroError('Purchase order status cannot move backwards', 400)
                if resource == 'Invoices' and existing['Status'] in ('PAID', 'VOIDED'):
                    raise XeroError('Invoice is not editable', 400)
                merged = dict(existing)
                merged.update(doc)
                store[merged[id_key]] = self._finish(merged)
                return {key: [merged]}
            doc[id_key] = self._id()
            n = next(self.counter)
            if resource == 'PurchaseOrders':
                doc.setdefault('PurchaseOrderNumber', f'PO-X{n:04d}')
            elif resource == 'Invoices':
                doc.setdefault('InvoiceNumber', f'INV-{n:04d}' if doc.get('Type') == 'ACCREC' else '')
            else:
                doc.setdefault('QuoteNumber', f'QU-{n:04d}')
            store[doc[id_key]] = self._finish(doc)
            return {key: [doc]}

        raise XeroError(f'Unsupported fake call {method} {path}')

    def _with_balances(self, resource, row):
        if resource == 'Contacts' and row['ContactID'] in self.balances:
            return dict(row, Balances=self.balances[row['ContactID']])
        return row
