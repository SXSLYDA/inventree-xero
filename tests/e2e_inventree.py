"""End-to-end test against a real InvenTree database, with a fake Xero organisation.

Run from InvenTree's backend directory (the plugin installed and activated, DB migrated):

    python manage.py shell -c "exec(open('/path/to/tests/e2e_inventree.py').read())"

Exits non-zero on the first failed check.
"""

import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, __file__.rsplit('/', 1)[0] if '__file__' in dir() else 'tests')

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402
from djmoney.money import Money  # noqa: E402

from company.models import Company, SupplierPart  # noqa: E402
from order.models import (  # noqa: E402
    PurchaseOrder,
    PurchaseOrderExtraLine,
    PurchaseOrderLineItem,
    SalesOrder,
    SalesOrderLineItem,
)
from part.models import Part  # noqa: E402
from plugin.registry import registry  # noqa: E402

from inventree_xero.mapping import xmeta  # noqa: E402
from inventree_xero.models import XeroConnection, XeroSyncLog  # noqa: E402
from inventree_xero.sync import XeroSync  # noqa: E402

try:
    from fake_xero import FakeXeroClient
except ImportError:  # run via exec() from another directory
    import importlib.util
    import os

    _spec = importlib.util.spec_from_file_location(
        'fake_xero', os.path.join(os.environ.get('XERO_TESTS', 'tests'), 'fake_xero.py'))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    FakeXeroClient = _mod.FakeXeroClient

CHECKS = []


def check(label, condition, detail=''):
    CHECKS.append(label)
    status = 'ok ' if condition else 'FAIL'
    print(f'[{status}] {label}' + (f' — {detail}' if detail and not condition else ''))
    if not condition:
        raise SystemExit(1)


from common.settings import set_global_setting  # noqa: E402

set_global_setting('INVENTREE_DEFAULT_CURRENCY', 'AUD')

plugin = registry.get_plugin('xero')
check('plugin loaded and active', plugin is not None)

import types  # noqa: E402

import inventree_xero.xero_client as xero_client_mod  # noqa: E402

xero_client_mod.time = types.SimpleNamespace(sleep=lambda seconds: None)  # retries without waiting

# Sections 1-10 exercise every feature, so they run LIVE with the permissive options switched on.
# Section 11 then checks the safe defaults (dry run, drafts only, no delete/void, holds, kill switch).
settings = {
    'WRITE_MODE': 'live', 'AUTO_APPROVE': True, 'ALLOW_DELETE_VOID': True, 'IMPORT_NEW_DOCUMENTS': True,
    'SHOP_QUOTE_AUTO_RESEND': True, 'CONTACT_MATCH': 'strict', 'REQUIRE_TAX_TYPE': True, 'CHECK_TOTALS': True,
    'ALERT_EMAILS': 'ops@example.com',
    'PO_SYNC_DIRECTION': 'both', 'SO_SYNC_DIRECTION': 'both', 'BILLS_MODE': 'on_receipt',
    'BILL_STATUS': 'AUTHORISED', 'MARK_PO_BILLED': True, 'CONTACT_SYNC': 'create_update',
    'SHOP_INTEGRATION': True, 'SHOP_AUTHORISE_PAID': True, 'SHOP_PAYMENT_ACCOUNT': '090',
    'PURCHASE_TAX_TYPE': 'INPUT', 'SALES_TAX_TYPE': 'OUTPUT', 'BALANCE_SYNC': True,
    'BALANCE_PARAMETER': 'Xero Outstanding', 'NON_PART_LINES': 'xero', 'IMPORT_SKIP_NO_PARTS': True,
    'CREATE_XERO_ITEMS': False, 'SO_AUTHORISE_ON': 'never',
}
for key, value in settings.items():
    plugin.set_setting(key, value)

conn = XeroConnection.get_solo()
conn.access_token = 'fake'
conn.refresh_token = 'fake'
conn.expires_at = timezone.now() + timedelta(hours=1)
conn.tenant_id = 'tenant'
conn.tenant_name = 'Demo Company (AU)'
conn.short_code = '!demo'
conn.import_marks = {}
conn.save()

fake = FakeXeroClient(plugin)

import inventree_xero.sync as sync_module  # noqa: E402

sync_module.XeroClient = lambda _plugin: fake  # event handlers build their own XeroSync
fake.store['Items']['item-1'] = {'ItemID': 'item-1', 'Code': '1-1234567', 'Name': 'TE connector'}


def run(direction='all'):
    sync = XeroSync(plugin, client=fake)
    result = sync.run(direction=direction, triggered_by='test')
    return sync, result


# ------------------------------------------------------------------ fixtures
te = Company.objects.create(name='TE Connectivity Test', is_supplier=True, email='orders@te.example', tax_id='12 345 678 901')
customer = Company.objects.create(name='Mine Site Pty Ltd', is_customer=True)
connector = Part.objects.create(name='1-1234567', description='Connector housing', purchaseable=True, salable=True)
harness = Part.objects.create(name='HAR-R1300G-01', description='Engine harness', salable=True, assembly=True)
sp = SupplierPart.objects.create(part=connector, supplier=te, SKU='1-1234567')

# ================================================================== 1. PO export
po = PurchaseOrder.objects.create(supplier=te, reference=PurchaseOrder.generate_reference(), supplier_reference='Q-778')
line = PurchaseOrderLineItem.objects.create(order=po, part=sp, quantity=100, purchase_price=Money('0.85', 'AUD'),
                                            discount=Decimal('10'))
PurchaseOrderExtraLine.objects.create(order=po, description='Handling fee', quantity=1, price=Money('12', 'AUD'))
po.place_order()
po.refresh_from_db()

sync, result = run('export')
check('export run ok', result['ok'], result)
meta = xmeta(PurchaseOrder.objects.get(pk=po.pk))
xpo = fake.store['PurchaseOrders'].get(meta.get('id'))
check('PO created in Xero', xpo is not None)
check('PO number is the InvenTree reference', xpo['PurchaseOrderNumber'] == po.reference)
check('PO approved in Xero', xpo['Status'] == 'AUTHORISED')
check('item code + discount + tax on line', xpo['LineItems'][0].get('ItemCode') == '1-1234567'
      and xpo['LineItems'][0].get('DiscountRate') == 10 and xpo['LineItems'][0].get('TaxType') == 'INPUT')
check('extra line exported', len(xpo['LineItems']) == 2)
check('owned line ids stored', len(meta['line_ids']) == 2)
check('supplier linked to new Xero contact', xmeta(Company.objects.get(pk=te.pk)).get('contact_id'))
contact = fake.store['Contacts'][xmeta(Company.objects.get(pk=te.pk))['contact_id']]
check('contact carries tax number', contact.get('TaxNumber') == '12 345 678 901')
check('part linked to Xero item', xmeta(Part.objects.get(pk=connector.pk)).get('item_id') == 'item-1')
po.refresh_from_db()
check('order link set to Xero', 'go.xero.com' in (po.link or ''))
check('description tagged with Xero number', f'[Xero {xpo["PurchaseOrderNumber"]}]' in po.description)

sync, result = run('export')
check('second export: nothing re-sent', sync.stats['exported'] == 0 and sync.stats['unchanged'] >= 1, dict(sync.stats))

# ================================================================== 2. Xero-only line preserved
freight = {'Description': 'Freight (added in Xero)', 'Quantity': 1, 'UnitAmount': 25, 'AccountCode': '425'}
fake.edit_in_xero('PurchaseOrders', xpo['PurchaseOrderID'], LineItems=xpo['LineItems'] + [freight])
sync, result = run('import')
po.refresh_from_db()
check('import notes the Xero-only line', (xmeta(po).get('xero_only') or {}).get('count') == 1, xmeta(po).get('xero_only'))

PurchaseOrderLineItem.objects.filter(pk=line.pk).update(quantity=120)
sync, result = run('export')
xpo = fake.store['PurchaseOrders'][meta['id']]
descs = [ln['Description'] for ln in xpo['LineItems']]
check('InvenTree change exported', any(ln.get('Quantity') == 120 for ln in xpo['LineItems']), xpo['LineItems'])
check('freight added in Xero survived the update', 'Freight (added in Xero)' in descs, descs)
check('still exactly one Xero-only line', (xmeta(PurchaseOrder.objects.get(pk=po.pk)).get('xero_only') or {}).get('count') == 1)

# ================================================================== 3. Bills
PurchaseOrderLineItem.objects.filter(pk=line.pk).update(received=50)
sync, result = run('export')
po.refresh_from_db()
bill = xmeta(po).get('bill') or {}
xbill = fake.store['Invoices'].get(bill.get('id'))
check('draft bill created on partial receipt', xbill is not None and xbill['Status'] == 'DRAFT' and xbill['Type'] == 'ACCPAY')
check('bill uses received quantity', xbill['LineItems'][0]['Quantity'] == 50, xbill['LineItems'][0])
check('bill reference has PO number', po.reference in xbill['Reference'])

supplier_freight = {'Description': 'Supplier freight on invoice', 'Quantity': 1, 'UnitAmount': 18, 'AccountCode': '425'}
fake.edit_in_xero('Invoices', xbill['InvoiceID'], LineItems=xbill['LineItems'] + [supplier_freight])
PurchaseOrderLineItem.objects.filter(pk=line.pk).update(received=120)
po.refresh_from_db()
po.complete_order()
po.refresh_from_db()
check('PO complete in InvenTree', XeroSync.status_name(XeroSync.po_status(), po.status) == 'complete')
sync, result = run('export')
po.refresh_from_db()
xbill = fake.store['Invoices'][bill['id']]
check('bill approved on completion', xbill['Status'] == 'AUTHORISED', xbill['Status'])
check('bill updated to full received qty', xbill['LineItems'][0]['Quantity'] == 120)
check('supplier freight kept on bill', any('Supplier freight' in ln['Description'] for ln in xbill['LineItems']))
check('Xero PO marked billed', fake.store['PurchaseOrders'][meta['id']]['Status'] == 'BILLED')
check('PO link now final', xmeta(po).get('final') is True)

# ================================================================== 4. Import: parts only, rest stays in Xero
acme = fake.add_contact('Acme Supplies Test')
fake.add_document('PurchaseOrders', {
    'Contact': {'ContactID': acme['ContactID']}, 'Status': 'AUTHORISED', 'PurchaseOrderNumber': 'PO-X9001',
    'Reference': 'ACME-55', 'DateString': date.today().isoformat(),
    'LineItems': [
        {'ItemCode': '1-1234567', 'Description': 'Connector', 'Quantity': 40, 'UnitAmount': 0.9},
        {'Description': 'Crimping labour', 'Quantity': 2, 'UnitAmount': 85},
    ],
})
plugin.set_setting('AUTO_CREATE_SUPPLIER_PARTS', True)
sync, result = run('import')
imported = PurchaseOrder.objects.filter(supplier_reference='ACME-55').first()
check('Xero PO imported', imported is not None, result)
check('only the part line imported', imported.lines.count() == 1 and imported.extra_lines.count() == 0)
check('imported PO placed (Xero approved)', XeroSync.status_name(XeroSync.po_status(), imported.status) == 'placed')
check('labour line kept in Xero only', (xmeta(imported).get('xero_only') or {}).get('count') == 1)
sync, result = run('export')
check('imported PO not pushed straight back', sync.stats['exported'] == 0, dict(sync.stats))

mine = fake.add_contact('Mine Site Pty Ltd', balances={
    'AccountsReceivable': {'Outstanding': 1540.5, 'Overdue': 220.0}, 'AccountsPayable': {'Outstanding': 0, 'Overdue': 0}})
fake.add_document('Invoices', {
    'Type': 'ACCREC', 'Contact': {'ContactID': mine['ContactID']}, 'Status': 'AUTHORISED', 'Reference': 'Interest',
    'LineItems': [{'Description': 'Interest on overdue account', 'Quantity': 1, 'UnitAmount': 22.5}],
})
fake.add_document('Invoices', {
    'Type': 'ACCREC', 'Contact': {'ContactID': mine['ContactID']}, 'Status': 'AUTHORISED', 'Reference': 'MS-PO-881',
    'LineItems': [
        {'ItemCode': '1-1234567', 'Description': 'Connector', 'Quantity': 10, 'UnitAmount': 2.5},
        {'Description': 'Site call-out labour', 'Quantity': 3, 'UnitAmount': 110},
    ],
})
before = SalesOrder.objects.count()
sync, result = run('import')
check('interest-only invoice not imported', not SalesOrder.objects.filter(customer_reference='Interest').exists())
so_imp = SalesOrder.objects.filter(customer_reference='MS-PO-881').first()
check('part invoice imported as sales order', so_imp is not None and SalesOrder.objects.count() == before + 1)
check('sales order holds the part line only', so_imp.lines.count() == 1 and so_imp.extra_lines.count() == 0)
check('sales order issued', XeroSync.status_name(XeroSync.so_status(), so_imp.status) == 'in_progress')
check('existing customer matched (no duplicate company)', Company.objects.filter(name='Mine Site Pty Ltd').count() == 1)

# ================================================================== 5. Balances
customer.refresh_from_db()
balance = xmeta(customer).get('balance') or {}
check('customer balance imported', balance.get('receivable_outstanding') == 1540.5 and balance.get('receivable_overdue') == 220.0, balance)
from common.models import Parameter  # noqa: E402

param = Parameter.objects.filter(model_id=customer.pk, template__name='Xero Outstanding').first()
check('balance parameter written', param is not None and param.data == '1540.50', getattr(param, 'data', None))

# ================================================================== 6. Web shop order
shop_so = SalesOrder.objects.create(
    customer=customer, reference=SalesOrder.generate_reference(), customer_reference='WC-1001',
    metadata={'woocommerce': {'order_id': 555, 'number': '1001', 'on_account': False}},
)
SalesOrderLineItem.objects.create(order=shop_so, part=harness, quantity=1, sale_price=Money('480', 'AUD'))
shop_so.issue_order()
ok, message = XeroSync(plugin, client=fake).export_single('so', shop_so.pk)
check('web order exported', ok, message)
shop_so.refresh_from_db()
smeta = xmeta(shop_so)
xinv = fake.store['Invoices'][smeta['id']]
check('paid web order invoice approved immediately', xinv['Status'] in ('AUTHORISED', 'PAID'), xinv['Status'])
check('online payment recorded against clearing account', bool(smeta.get('payment_recorded')))
check('invoice paid in Xero', xinv['Status'] == 'PAID' and xinv['AmountDue'] == 0)
check('payment account used', list(fake.store['Payments'].values())[-1]['Account']['Code'] == '090')

account_so = SalesOrder.objects.create(
    customer=customer, reference=SalesOrder.generate_reference(), customer_reference='PO-5521',
    metadata={'woocommerce': {'order_id': 556, 'number': '1002', 'on_account': True}},
)
SalesOrderLineItem.objects.create(order=account_so, part=harness, quantity=2, sale_price=Money('480', 'AUD'))
account_so.issue_order()
plugin.process_event('woocommerce.salesorder_created', id=account_so.pk, on_account=True)
account_so.refresh_from_db()
ameta = xmeta(account_so)
check('shop event exports the account order', bool(ameta.get('id')))
check('account order invoice stays draft (normal rule)', fake.store['Invoices'][ameta['id']]['Status'] == 'DRAFT')
check('no payment recorded for account order', not ameta.get('payment_recorded'))

# ================================================================== 7. Cancel with Xero-only lines
inv = fake.store['Invoices'][ameta['id']]
fake.edit_in_xero('Invoices', inv['InvoiceID'], LineItems=inv['LineItems'] + [{'Description': 'Delivery', 'Quantity': 1, 'UnitAmount': 30}])
run('import')
account_so.refresh_from_db()
account_so.cancel_order()
logs_before = XeroSyncLog.objects.filter(level='warning').count()
XeroSync(plugin, client=fake).export_single('so', account_so.pk)
check('cancel leaves invoice with Xero-only lines alone', fake.store['Invoices'][ameta['id']]['Status'] == 'DRAFT')
check('warning logged for manual action', XeroSyncLog.objects.filter(level='warning').count() > logs_before)

# ================================================================== 8. Contact detail updates
Company.objects.filter(pk=te.pk).update(phone='08 9000 1234')
sync, result = run('export')
check('changed company details pushed to Xero contact',
      contact.get('Phones', [{}])[0].get('PhoneNumber') == '08 9000 1234', contact.get('Phones'))

# ================================================================== 9. API + search
User = get_user_model()
admin = User.objects.filter(username='xero-e2e').first() or User.objects.create_superuser('xero-e2e', 'a@b.c', 'pw-e2e-123')
client = Client()
client.force_login(admin)
r = client.get('/plugin/xero/api/search/', {'q': '1001'})
check('search API answers', r.status_code == 200, r.content[:300])
refs = [row['reference'] for row in r.json()['results']]
check('search finds the web order by shop number', shop_so.reference in refs, refs)
r = client.get('/plugin/xero/api/search/', {'q': fake.store['Invoices'][bill['id']]['InvoiceNumber'] or po.reference, 'type': 'po'})
check('search finds PO', po.reference in [row['reference'] for row in r.json()['results']])
r = client.get(f'/plugin/xero/api/order/po/{po.pk}/')
check('order panel API has bill', r.status_code == 200 and r.json()['bill']['linked'], r.content[:300])
r = client.get(f'/plugin/xero/api/company/{customer.pk}/')
check('company panel API has balance', r.json()['xero']['balance']['receivable_overdue'] == 220.0)
r = client.get(f'/plugin/xero/api/part/{connector.pk}/')
check('part panel API has item link', r.json()['xero']['item_code'] == '1-1234567')
r = client.get('/plugin/xero/api/status/')
check('status API answers', r.status_code == 200 and r.json()['connected'])
panels = plugin.get_ui_panels(None, {'target_model': 'part', 'target_id': connector.pk})
check('part panel registered', panels and panels[0]['source'].endswith('xero_ui.js:renderXeroPanel'))

# ================================================================== 10. Web shop quote requests <-> Xero quotes
# The shop plugin (inventree-woocommerce) is real; only its calls out to WooCommerce and WordPress are faked.
import inventree_woocommerce.quotes as shop_quotes_mod  # noqa: E402
import inventree_woocommerce.shop_link as shop_link  # noqa: E402
import inventree_woocommerce.woo_client as woo_client_mod  # noqa: E402
from inventree_woocommerce.config import load_config as shop_config  # noqa: E402
from inventree_woocommerce.models import QuoteItem, QuoteRequest  # noqa: E402

ACTIONS = []
NOTIFICATIONS = []
_real_apply_action = shop_quotes_mod.apply_action


def recording_apply_action(quote, data, config):
    ACTIONS.append((quote.reference, data.get('action'), data))
    return _real_apply_action(quote, data, config)


shop_quotes_mod.apply_action = recording_apply_action
shop_link.send_notification = lambda event, payload: NOTIFICATIONS.append((event, payload.get('reference')))


class FakeWoo:
    calls = []
    next_id = 900

    @classmethod
    def from_config(cls, config):
        return cls()

    def find_product_by_sku(self, sku):
        return None

    def get(self, path, **params):
        return {'id': 1, 'status': 'pending', 'date_paid': None}

    def post(self, path, data):
        FakeWoo.calls.append(('post', path))
        FakeWoo.next_id += 1
        return {'id': FakeWoo.next_id}

    def put(self, path, data):
        FakeWoo.calls.append(('put', path, data))
        return {}


woo_client_mod.WooClient = FakeWoo
Part.objects.filter(pk=harness.pk).update(bom_validated=True)  # build-to-order designs must be released to quote

from inventree_xero.models import XeroQuoteLink  # noqa: E402

plugin.set_setting('SHOP_QUOTES', 'draft')
plugin.set_setting('SHOP_QUOTE_XERO_EDITS', True)
today = date.today()

qr = QuoteRequest.objects.create(reference='QR-261020-AB12', woo_customer_id=77, company_id=customer.pk,
                                 name='Sam', email='sam@mine.example')
QuoteItem.objects.create(request=qr, position=1, kind='copy', description='Engine harness copy',
                         machine='CAT R1300G', quantity=2)
QuoteItem.objects.create(request=qr, position=2, kind='component', description='Connector housings', quantity=10)
plugin.process_event('woocommerce.quote_received', id=qr.pk, reference=qr.reference)
link = XeroQuoteLink.objects.get(quote_request_id=qr.pk)
xq = fake.store['Quotes'][link.xero_id]
check('quote request becomes a Xero draft quote', xq['Status'] == 'DRAFT' and xq['Reference'] == qr.reference)
check('draft has a placeholder line per requested item',
      [ln['Description'][:7] for ln in xq['LineItems']] == ['Item 1:', 'Item 2:'] and xq['Total'] == 0, xq['LineItems'])
check('quote contact is the customer company', xq['Contact']['ContactID'] == xmeta(Company.objects.get(pk=customer.pk))['contact_id'])

# Price it in Xero
fake.edit_in_xero('Quotes', link.xero_id, LineItems=[
    {'ItemCode': 'HAR-R1300G-01', 'Description': 'Item 1: Engine harness copy', 'Quantity': 2, 'UnitAmount': 1450},
    {'ItemCode': '1-1234567', 'Description': 'Item 2: Connector housings (lead time In stock)', 'Quantity': 10, 'UnitAmount': 4.2},
])
run('import')
qr.refresh_from_db()
lines = list(qr.lines.order_by('position'))
check('Xero pricing copied to the shop quote', len(lines) == 2 and lines[0].unit_price == Decimal('1450'), [(l.part_id, l.unit_price) for l in lines])
check('lines tied to the customer\'s items', lines[0].item.position == 1 and lines[1].item.position == 2)
check('lead time carried over', lines[1].lead_time == 'In stock' and lines[1].description == 'Connector housings')
check('pricing alone does not send it', qr.status == 'new')

# Mark it Sent in Xero -> the shop sends it to the customer
fake.edit_in_xero('Quotes', link.xero_id, Status='SENT', ExpiryDate=(today + timedelta(days=14)).isoformat())
run('import')
qr.refresh_from_db()
check('Sent in Xero → shop sends quote to customer', qr.status == 'quoted' and ('quote_sent', qr.reference) in NOTIFICATIONS)
check('validity follows the Xero expiry date', qr.valid_until == today + timedelta(days=14), qr.valid_until)
sync, result = run('export')
check('nothing bounced back to Xero', sync.stats['exported'] == 0, dict(sync.stats))

# Customer wants a better price: edit the sent quote in Xero
xq = fake.store['Quotes'][link.xero_id]
revised = [dict(ln) for ln in xq['LineItems']]
revised[0]['UnitAmount'] = 1400
fake.edit_in_xero('Quotes', link.xero_id, LineItems=revised)
before = len(ACTIONS)
run('import')
qr.refresh_from_db()
check('revision: shop reopens, updates and re-sends', [a[1] for a in ACTIONS[before:]] == ['reopen', 'set_lines', 'send'],
      ACTIONS[before:])
check('customer sees the revised price', qr.status == 'quoted' and qr.lines.order_by('position').first().unit_price == Decimal('1400'))

# A labour line with no part blocks the update until it has one
fake.edit_in_xero('Quotes', link.xero_id, LineItems=fake.store['Quotes'][link.xero_id]['LineItems']
                  + [{'Description': 'Site visit labour', 'Quantity': 1, 'UnitAmount': 180}])
run('import')
link.refresh_from_db()
qr.refresh_from_db()
check('non-part quote line flagged, shop left alone', 'no salable InvenTree part' in link.error and qr.lines.count() == 2, link.error)
Part.objects.create(name='LABOUR', description='Labour / service', salable=True)
plugin.set_setting('SHOP_QUOTE_SERVICE_PART', 'LABOUR')
ok, message = XeroSync(plugin, client=fake).shop_quotes.safe(XeroSync(plugin, client=fake).shop_quotes.pull, qr)
link.refresh_from_db()
qr.refresh_from_db()
check('with a service part the labour line reaches the shop', ok and qr.lines.count() == 3 and not link.error, message)

# Changed in the shop widget instead -> Xero follows
cfg = shop_config()
shop_quotes_mod.apply_action(qr, {'action': 'reopen'}, cfg)
rows = [{'part_id': l.part_id, 'quantity': l.quantity, 'unit_price': str(l.unit_price), 'item_id': l.item_id,
         'description': l.description, 'lead_time': l.lead_time} for l in qr.lines.order_by('position')[:2]]
rows[1]['quantity'] = 12
shop_quotes_mod.apply_action(qr, {'action': 'set_lines', 'lines': rows}, cfg)
shop_quotes_mod.apply_action(qr, {'action': 'send', 'valid_days': 21}, cfg)
run('export')
xq = fake.store['Quotes'][link.xero_id]
check('shop edit pushed to the Xero quote', len(xq['LineItems']) == 2 and xq['LineItems'][1]['Quantity'] == 12 and xq['Status'] == 'SENT',
      [(ln.get('ItemCode'), ln['Quantity']) for ln in xq['LineItems']])
check('Xero expiry follows the shop', xq['ExpiryDate'] == (today + timedelta(days=21)).isoformat())

# Customer accepts and charges it to account -> Sales Order -> invoice; quote Accepted then Invoiced
so_q = SalesOrder.objects.create(customer=customer, reference=SalesOrder.generate_reference(), customer_reference=qr.reference,
                                 metadata={'woocommerce': {'order_id': 777, 'number': '1003', 'on_account': True}})
SalesOrderLineItem.objects.create(order=so_q, part=harness, quantity=2, sale_price=Money('1400', 'AUD'))
SalesOrderLineItem.objects.create(order=so_q, part=connector, quantity=12, sale_price=Money('4.2', 'AUD'))
QuoteRequest.objects.filter(pk=qr.pk).update(status='accepted', sales_order_id=so_q.pk)
so_q.issue_order()
plugin.process_event('woocommerce.quote_accepted', id=qr.pk, reference=qr.reference, sales_order=so_q.pk)
link.refresh_from_db()
check('accepted in the shop → Xero quote Accepted', fake.store['Quotes'][link.xero_id]['Status'] == 'ACCEPTED' and link.sales_order_id == so_q.pk)
plugin.process_event('woocommerce.salesorder_created', id=so_q.pk, on_account=True)
link.refresh_from_db()
so_q.refresh_from_db()
check('accepted quote invoiced, Xero quote marked Invoiced',
      xmeta(so_q).get('doc_type') == 'invoice' and fake.store['Quotes'][link.xero_id]['Status'] == 'INVOICED' and link.final)
r = client.get(f'/plugin/xero/api/order/so/{so_q.pk}/')
check('sales order panel shows the quote it came from', r.json().get('quote', {}).get('reference') == qr.reference, r.json().get('quote'))

# Not approved (1): declined from Xero — needs a reason the customer can read
qr2 = QuoteRequest.objects.create(reference='QR-261020-CD34', woo_customer_id=77, company_id=customer.pk, name='Sam')
it = QuoteItem.objects.create(request=qr2, position=1, kind='component', quantity=5)
shop_quotes_mod.apply_action(qr2, {'action': 'set_lines', 'lines': [{'part_id': connector.pk, 'quantity': 5, 'unit_price': '4.5', 'item_id': it.pk}]}, cfg)
shop_quotes_mod.apply_action(qr2, {'action': 'send'}, cfg)
run('export')
link2 = XeroQuoteLink.objects.get(quote_request_id=qr2.pk)
check('quote sent from the shop appears in Xero as Sent', fake.store['Quotes'][link2.xero_id]['Status'] == 'SENT')
fake.edit_in_xero('Quotes', link2.xero_id, Status='DECLINED')
run('import')
qr2.refresh_from_db()
link2.refresh_from_db()
check('declined in Xero without a reason: held, warning shown', qr2.status == 'quoted' and 'no reason' in link2.error, link2.error)
fake.edit_in_xero('Quotes', link2.xero_id, Summary='We can no longer source this connector, sorry.')
run('import')
qr2.refresh_from_db()
link2.refresh_from_db()
check('declined in Xero with a Summary → shop declines, customer told',
      qr2.status == 'declined' and qr2.decline_reason.startswith('We can no longer') and link2.final)

# Not approved (2): the customer never accepts and the shop expires it
qr3 = QuoteRequest.objects.create(reference='QR-261020-EF56', woo_customer_id=77, company_id=customer.pk, name='Sam')
it3 = QuoteItem.objects.create(request=qr3, position=1, kind='component', quantity=3)
shop_quotes_mod.apply_action(qr3, {'action': 'set_lines', 'lines': [{'part_id': connector.pk, 'quantity': 3, 'unit_price': '4.5', 'item_id': it3.pk}]}, cfg)
shop_quotes_mod.apply_action(qr3, {'action': 'send'}, cfg)
run('export')
QuoteRequest.objects.filter(pk=qr3.pk).update(status='expired')
run('export')
link3 = XeroQuoteLink.objects.get(quote_request_id=qr3.pk)
check('expired in the shop → Xero quote Declined and closed', fake.store['Quotes'][link3.xero_id]['Status'] == 'DECLINED' and link3.final)

# Quote mode 'sent': unpriced requests stay out of Xero
plugin.set_setting('SHOP_QUOTES', 'sent')
qr4 = QuoteRequest.objects.create(reference='QR-261020-GH78', woo_customer_id=77, name='Walk-in', company_name='New Mine Co')
QuoteItem.objects.create(request=qr4, position=1, kind='custom', description='Loom', quantity=1)
run('export')
check('"only once sent" mode leaves new requests out of Xero', not XeroQuoteLink.objects.filter(quote_request_id=qr4.pk, xero_id__gt='').exists())
plugin.set_setting('SHOP_QUOTES', 'draft')

r = client.get('/plugin/xero/api/quotes/', {'q': 'QR-261020'})
found = {row['reference']: row for row in r.json()['results']}
check('quotes API lists shop quotes with Xero numbers', found['QR-261020-AB12']['xero']['status'] == 'INVOICED' and len(found) == 4, list(found))

# Web orders can follow the sales-order document setting instead of always being invoices
plugin.set_setting('SO_XERO_DOCUMENT', 'QUOTE')
plugin.set_setting('SHOP_ORDER_DOCUMENT', 'follow')
so_f = SalesOrder.objects.create(customer=customer, reference=SalesOrder.generate_reference(), customer_reference='WC-1004',
                                 metadata={'woocommerce': {'order_id': 778, 'number': '1004', 'on_account': True}})
SalesOrderLineItem.objects.create(order=so_f, part=harness, quantity=1, sale_price=Money('480', 'AUD'))
so_f.issue_order()
XeroSync(plugin, client=fake).export_single('so', so_f.pk)
so_f.refresh_from_db()
check('"same as sales orders" sends a web order as a Xero quote', xmeta(so_f).get('doc_type') == 'quote', xmeta(so_f))
plugin.set_setting('SO_XERO_DOCUMENT', 'INVOICE')
plugin.set_setting('SHOP_ORDER_DOCUMENT', 'invoice')

# Both plugins together: the shop's credit check uses the Xero balance without double counting
from inventree_woocommerce.accounts import credit_used  # noqa: E402

shop_plugin = registry.get_plugin('woocommerce')
used = credit_used(customer.pk, 'AUD', include_xero=True)
# Xero says 1540.50 owing. Open account orders not yet approved in Xero: the quote order (draft invoice, 2850.40)
# and the order sent as a quote (480). Skipped: the paid-online web order, and the order whose invoice is approved.
check('shop credit used = Xero balance + open orders not yet invoiced', used == Decimal('4870.90'), used)
check('without Xero the shop falls back to open account orders', credit_used(customer.pk, 'AUD', include_xero=False) == Decimal('3355.40'),
      credit_used(customer.pk, 'AUD', include_xero=False))
check('shop refreshes customer credit when Xero balances change', shop_plugin.wants_process_event('xero.balances_updated'))

# ================================================================== 10. Event wiring (InvenTree 1.6 names)
from order.events import PurchaseOrderEvents, SalesOrderEvents  # noqa: E402

check('wants PO placed event', plugin.wants_process_event(PurchaseOrderEvents.PLACED))
check('wants SO shipment event', plugin.wants_process_event(SalesOrderEvents.SHIPMENT_COMPLETE))
check('wants SO hold event', plugin.wants_process_event(SalesOrderEvents.HOLD))
check('wants receipt event when billing on receipt', plugin.wants_process_event(PurchaseOrderEvents.ITEM_RECEIVED))
plugin.set_setting('BILLS_MODE', 'off')
check('ignores receipt event when bills off', not plugin.wants_process_event(PurchaseOrderEvents.ITEM_RECEIVED))
check('wants shop event', plugin.wants_process_event('woocommerce.salesorder_created'))
check('scheduled task registered', 'xero_sync' in plugin.get_scheduled_tasks())

# ================================================================== 11. Safe mode (the defaults)
from django.contrib.auth.models import Permission  # noqa: E402

import inventree_xero.alerts as alerts_mod  # noqa: E402
from inventree_xero.crypto import is_encrypted  # noqa: E402
from inventree_xero.models import XeroWriteAudit  # noqa: E402
from inventree_xero.xero_client import required_scopes  # noqa: E402

EMAILS = []
alerts_mod._deliver = lambda subject, body, to: (EMAILS.append((subject, body, to)) or (True, None))

for key in ('AUTO_APPROVE', 'ALLOW_DELETE_VOID', 'IMPORT_NEW_DOCUMENTS', 'SHOP_QUOTE_AUTO_RESEND'):
    plugin.set_setting(key, False)
plugin.set_setting('WRITE_MODE', 'dry_run')
plugin.set_setting('PO_PLACED_XERO_STATUS', 'AUTHORISED')
plugin.set_setting('BILLS_MODE', 'off')


def new_po(supplier, sp_obj, price='2', qty=10):
    order = PurchaseOrder.objects.create(supplier=supplier, reference=PurchaseOrder.generate_reference())
    PurchaseOrderLineItem.objects.create(order=order, part=sp_obj, quantity=qty,
                                         purchase_price=Money(price, 'AUD') if price is not None else None)
    order.place_order()
    order.refresh_from_db()
    return order


safe_sup = Company.objects.create(name='Safe Supplies Test', is_supplier=True)
sp_safe = SupplierPart.objects.create(part=connector, supplier=safe_sup, SKU='SAFE-1')
po_s = new_po(safe_sup, sp_safe)
pos_before, contacts_before = len(fake.store['PurchaseOrders']), len(fake.store['Contacts'])
dry_before = XeroWriteAudit.objects.filter(mode='dry_run').count()
sync, result = run('export')
po_s.refresh_from_db()
check('dry run: nothing written to Xero', len(fake.store['PurchaseOrders']) == pos_before
      and len(fake.store['Contacts']) == contacts_before)
check('dry run: shows what it would send', 'create purchase order: 1 line(s), 20.00' in (xmeta(po_s).get('dry_run') or {}).get('would', ''),
      xmeta(po_s))
check('dry run: intended writes audited', XeroWriteAudit.objects.filter(mode='dry_run').count() > dry_before)
check('dry run: sync summary says so', result['message'].startswith('DRY RUN'), result['message'])

plugin.set_setting('WRITE_MODE', 'live')
run('export')
po_s.refresh_from_db()
meta_s = xmeta(po_s)
check('drafts only: a placed PO goes to Xero as Draft', fake.store['PurchaseOrders'][meta_s['id']]['Status'] == 'DRAFT')
check('live writes audited with Xero\'s reply', XeroWriteAudit.objects.filter(
    mode='live', inventree_ref=po_s.reference, ok=True, response_status=200).exclude(idempotency_key='').exists())
check('every write carries an Idempotency-Key', all(h.get('Idempotency-Key') for m, u, h in fake.headers_seen if m != 'GET'))

fake.edit_in_xero('PurchaseOrders', meta_s['id'], Status='AUTHORISED')  # a person approves it in Xero
run('import')
PurchaseOrderLineItem.objects.filter(order=po_s).update(quantity=12)
run('export')
po_s.refresh_from_db()
check('change after approval in Xero is held, not written over it',
      'approved in Xero' in (xmeta(po_s).get('held') or {}).get('reason', '')
      and fake.store['PurchaseOrders'][meta_s['id']]['LineItems'][0]['Quantity'] == 10, xmeta(po_s).get('held'))
po_s.cancel_order()
run('export')
po_s.refresh_from_db()
check('cancel never deletes in Xero - held for a person', fake.store['PurchaseOrders'][meta_s['id']]['Status'] == 'AUTHORISED'
      and 'delete/void is off' in xmeta(po_s)['held']['reason'], xmeta(po_s).get('held'))

before = len(fake.store['PurchaseOrders'])
po_free = new_po(te, sp, price=None)
run('export')
po_free.refresh_from_db()
check('unpriced line: held, not sent', 'has no price' in (xmeta(po_free).get('held') or {}).get('reason', '')
      and len(fake.store['PurchaseOrders']) == before, xmeta(po_free).get('held'))
plugin.set_setting('PURCHASE_TAX_TYPE', '')
po_tax = new_po(te, sp)
run('export')
po_tax.refresh_from_db()
check('missing tax type: held, GST never guessed', 'tax type is not set' in (xmeta(po_tax).get('held') or {}).get('reason', ''))
plugin.set_setting('PURCHASE_TAX_TYPE', 'INPUT')
run('export')
po_tax.refresh_from_db()
check('fixed setting: held order goes through and the hold clears', xmeta(po_tax).get('id') and not xmeta(po_tax).get('held'))

r = client.get(f'/plugin/xero/api/order/po/{po_free.pk}/preview/')
check('preview shows lines and what would stop it, writing nothing',
      r.status_code == 200 and any('has no price' in p for p in r.json()['problems']) and r.json()['payload']['lines'],
      r.content[:300])

# Contacts: anything uncertain is held for a person
fake.add_contact('Dup Wire Co')
fake.add_contact('Dup Wire Co')
dup = Company.objects.create(name='Dup Wire Co', is_supplier=True)
po_dup = new_po(dup, SupplierPart.objects.create(part=connector, supplier=dup, SKU='DUP-1'))
fake.add_contact('Acme Cables Pty Ltd')
near = Company.objects.create(name='Acme Cables', is_supplier=True)
po_near = new_po(near, SupplierPart.objects.create(part=connector, supplier=near, SKU='AC-1'))
diff_contact = fake.add_contact('Email Diff Ltd', EmailAddress='accounts@other.example')
diff = Company.objects.create(name='Email Diff Ltd', is_supplier=True, email='orders@emaildiff.example')
po_diff = new_po(diff, SupplierPart.objects.create(part=connector, supplier=diff, SKU='ED-1'))
contacts_before = len(fake.store['Contacts'])
run('export')
for order, words in ((po_dup, 'Xero contacts are named'), (po_near, 'rather than creating a duplicate'),
                     (po_diff, 'different email')):
    order.refresh_from_db()
    check(f'uncertain contact held ({words})', words in (xmeta(order).get('held') or {}).get('reason', '')
          and not xmeta(order).get('id'), xmeta(order).get('held'))
check('no duplicate contacts created', len(fake.store['Contacts']) == contacts_before)
r = client.post(f'/plugin/xero/api/company/{diff.pk}/', data={'action': 'link_id', 'contact_id': diff_contact['ContactID']},
                content_type='application/json')
check('contact linked by hand', r.status_code == 200 and r.json()['xero']['contact_id'] == diff_contact['ContactID'], r.content[:200])
run('export')
po_diff.refresh_from_db()
check('after linking by hand the order goes through', xmeta(po_diff).get('id') and not xmeta(po_diff).get('held'))
r = client.get(f'/plugin/xero/api/company/{near.pk}/candidates/')
check('contact finder lists the near match', any(c['name'] == 'Acme Cables Pty Ltd' for c in r.json()['results']))

# Lost reply: Xero saved it but the response never arrived - the retry must not make a second PO
fake.drop_after_commit = 1
before = len(fake.store['PurchaseOrders'])
po_net = new_po(te, sp)
run('export')
po_net.refresh_from_db()
check('network drop after save: exactly one Xero PO (idempotency)', len(fake.store['PurchaseOrders']) == before + 1
      and xmeta(po_net).get('id') in fake.store['PurchaseOrders'])

# Kill switch
plugin.set_setting('WRITE_MODE', 'paused')
calls_before = len(fake.calls)
po_p = new_po(te, sp)
result = run('export')[1]
plugin.process_event('purchaseorder.placed', id=po_p.pk)
r = client.post(f'/plugin/xero/api/order/po/{po_p.pk}/push/')
check('paused: sync, events and manual pushes send nothing', len(fake.calls) == calls_before and 'paused' in result['message']
      and r.status_code == 400 and 'paused' in r.json().get('message', ''), (result, r.content[:200]))
r = client.post('/plugin/xero/api/mode/', data={'mode': 'live'}, content_type='application/json')
check('going live needs explicit confirmation', r.status_code == 400)
r = client.post('/plugin/xero/api/mode/', data={'mode': 'live', 'confirm': True}, content_type='application/json')
check('kill switch released from the API', r.status_code == 200 and r.json()['write_mode'] == 'live')

# Who can do what
viewer = User.objects.create_user('xero-viewer', 'v@b.c', 'pw-viewer-123')
viewer.user_permissions.add(*Permission.objects.filter(codename__in=['view_purchaseorder', 'change_purchaseorder']))
vclient = Client()
vclient.force_login(viewer)
check('non-staff can view the Xero panel', vclient.get(f'/plugin/xero/api/order/po/{po_s.pk}/').status_code == 200)
check('non-staff cannot push to Xero', vclient.post(f'/plugin/xero/api/order/po/{po_p.pk}/push/').status_code == 403)
check('non-staff cannot change the write mode', vclient.post('/plugin/xero/api/mode/', data={'mode': 'live', 'confirm': True},
                                                             content_type='application/json').status_code == 403)

# Secrets and permissions
raw = XeroConnection.objects.values_list('access_token', flat=True).get()
check('Xero tokens encrypted at rest', is_encrypted(raw) and 'fake' not in raw and XeroConnection.get_solo().access_token_value == 'fake')
scopes = required_scopes(plugin.get_setting)
check('only the Xero permissions the features need', 'accounting.payments' not in scopes and 'openid' not in scopes
      and 'accounting.settings.read' in scopes, scopes)
plugin.set_setting('AUTO_APPROVE', True)
check('payments permission only when payments are recorded', 'accounting.payments' in required_scopes(plugin.get_setting))
plugin.set_setting('AUTO_APPROVE', False)

# Alerts
EMAILS.clear()
plugin.set_setting('SALES_TAX_TYPE', '')
so_alert = SalesOrder.objects.create(customer=customer, reference=SalesOrder.generate_reference())
SalesOrderLineItem.objects.create(order=so_alert, part=harness, quantity=1, sale_price=Money('99', 'AUD'))
so_alert.issue_order()
run('export')
check('held documents emailed to the alert address', EMAILS and EMAILS[-1][2] == ['ops@example.com']
      and so_alert.reference in EMAILS[-1][1] and 'HELD' in EMAILS[-1][1], EMAILS[-1:] or 'no email')
n_emails = len(EMAILS)
run('export')
check('the same problem is only emailed once a day', len(EMAILS) == n_emails)
plugin.set_setting('SALES_TAX_TYPE', 'OUTPUT')

# Quotes: a revision made in Xero waits for you to mark it Sent again
qr5 = QuoteRequest.objects.create(reference='QR-261020-JK90', woo_customer_id=77, company_id=customer.pk, name='Sam')
it5 = QuoteItem.objects.create(request=qr5, position=1, kind='component', quantity=4)
shop_quotes_mod.apply_action(qr5, {'action': 'set_lines', 'lines': [{'part_id': connector.pk, 'quantity': 4,
                                                                     'unit_price': '4.5', 'item_id': it5.pk}]}, cfg)
shop_quotes_mod.apply_action(qr5, {'action': 'send'}, cfg)
run('export')
link5 = XeroQuoteLink.objects.get(quote_request_id=qr5.pk)
lines5 = [dict(ln) for ln in fake.store['Quotes'][link5.xero_id]['LineItems']]
lines5[0]['UnitAmount'] = 4.1
fake.edit_in_xero('Quotes', link5.xero_id, LineItems=lines5)
before = len(ACTIONS)
run('import')
qr5.refresh_from_db()
link5.refresh_from_db()
check('revised in Xero: shop quote updated but not re-sent', [a[1] for a in ACTIONS[before:]] == ['reopen', 'set_lines']
      and qr5.status == 'reviewing', ACTIONS[before:])
check('…and the Xero quote is back to Draft, waiting for you', fake.store['Quotes'][link5.xero_id]['Status'] == 'DRAFT')
fake.edit_in_xero('Quotes', link5.xero_id, Status='SENT')
run('import')
qr5.refresh_from_db()
check('marking it Sent in Xero sends the revision', qr5.status == 'quoted' and qr5.lines.first().unit_price == Decimal('4.10'))

plugin.set_setting('WRITE_MODE', 'dry_run')
fake.edit_in_xero('Quotes', link5.xero_id, Status='DRAFT')
before, notes_before = len(ACTIONS), len(NOTIFICATIONS)
run('import')
qr5.refresh_from_db()
check('dry run never acts on shop quotes or emails customers', len(ACTIONS) == before and len(NOTIFICATIONS) == notes_before
      and qr5.status == 'quoted')
plugin.set_setting('WRITE_MODE', 'live')

# ================================================================== 12. Configuration check and live self-test
import InvenTree.status  # noqa: E402

import inventree_xero.selftest as selftest_mod  # noqa: E402
from inventree_xero.checks import check_configuration  # noqa: E402

InvenTree.status.is_worker_running = lambda **kwargs: True  # no worker in this test process
report = check_configuration(plugin, client=fake)
levels = {i['name']: i['level'] for i in report.items}
check('config check passes on a correct setup', report.passed, report.as_text())
check('config check verifies accounts and tax types in Xero', levels.get('Purchase account') == 'ok'
      and levels.get('Sales tax type') == 'ok' and levels.get('Shop payment account') == 'ok', levels)
plugin.set_setting('PURCHASE_ACCOUNT_CODE', '999')
plugin.set_setting('SALES_TAX_TYPE', 'NOPE')
report = check_configuration(plugin, client=fake)
fails = [i['name'] for i in report.items if i['level'] == 'fail']
check('config check catches a wrong account code and tax type', 'Purchase account' in fails and 'Sales tax type' in fails, fails)
plugin.set_setting('PURCHASE_ACCOUNT_CODE', '300')
plugin.set_setting('SALES_TAX_TYPE', 'OUTPUT')
writes_before = XeroWriteAudit.objects.count()
check_configuration(plugin, client=fake)
check('config check writes nothing', XeroWriteAudit.objects.count() == writes_before)

plugin.set_setting('WRITE_MODE', 'dry_run')
docs_before = {k: len(v) for k, v in fake.store.items()}
report = selftest_mod.run_selftest(plugin, confirm_demo=True, client=fake)
counts = report.counts()
check('live self-test passes against a Xero that behaves as expected', report.passed and counts['fail'] == 0, report.as_text())
check('self-test covers every flow', counts['ok'] >= 25, counts)
check('self-test writes even in dry run (Demo Company only) and audits it',
      len(fake.store['Quotes']) > docs_before['Quotes'] and XeroWriteAudit.objects.filter(doc_type='selftest', mode='live').exists())
check('self-test archives its contact and deletes its item',
      all(c.get('ContactStatus') == 'ARCHIVED' for c in fake.store['Contacts'].values() if 'self-test' in c['Name'])
      and not any(str(i.get('Code', '')).startswith('IXT') for i in fake.store['Items'].values()))
check('client back to dry run after the self-test', fake.dry_run and not fake.force_live)

fake.org.update(IsDemoCompany=False, Class='STANDARD', Name='OMG Harness Pty Ltd')
docs_before = {k: len(v) for k, v in fake.store.items()}
report = selftest_mod.run_selftest(plugin, confirm_demo=True, client=fake)
check('self-test refuses a real organisation and writes nothing', not report.passed
      and 'not a Xero Demo Company' in report.as_text() and {k: len(v) for k, v in fake.store.items()} == docs_before)
fake.org.update(IsDemoCompany=True, Class='DEMO', Name='Demo Company (AU)')
report = selftest_mod.run_selftest(plugin, confirm_demo=False, client=fake)
check('self-test needs explicit confirmation', not report.passed and 'Confirm' in report.as_text())

# The same tools from the API and the command line
_real_client = xero_client_mod.XeroClient
xero_client_mod.XeroClient = lambda _plugin: fake
r = client.post('/plugin/xero/api/check/')
check('configuration check from the API', r.status_code == 200 and r.json()['passed'], r.content[:300])
r = client.post('/plugin/xero/api/selftest/', data={'confirm': 'nope'}, content_type='application/json')
check('self-test API needs DEMO typed to confirm', r.status_code == 400)
import io  # noqa: E402

from django.core.management import call_command  # noqa: E402

out = io.StringIO()
call_command('xero_check', stdout=out)
check('manage.py xero_check prints the report', 'configuration check' in out.getvalue() and '[PASS]' in out.getvalue())
xero_client_mod.XeroClient = _real_client
plugin.set_setting('WRITE_MODE', 'live')

print(f'\nALL {len(CHECKS)} END-TO-END CHECKS PASSED')
