"""InvenTree plugin entry point: Xero sync for purchase orders, bills, sales orders, contacts and items."""

import logging
from datetime import timedelta

from django.core.validators import MinValueValidator
from django.utils import timezone

from plugin import InvenTreePlugin
from plugin.mixins import AppMixin, EventMixin, ScheduleMixin, SettingsMixin, UrlsMixin, UserInterfaceMixin

from . import PLUGIN_VERSION

logger = logging.getLogger('inventree')

# InvenTree / plugin events that push one order straight to Xero (InvenTree 1.6 event names)
ORDER_EVENTS = {
    'purchaseorder.placed': 'po',
    'purchaseorder.completed': 'po',
    'purchaseorder.cancelled': 'po',
    'purchaseorder.hold': 'po',
    'salesorder.issued': 'so',
    'salesorder.hold': 'so',
    'salesorder.completed': 'so',
    'salesorder.cancelled': 'so',
    'salesordershipment.completed': 'shipment',  # id = shipment
}
RECEIPT_EVENT = 'purchaseorderitem.received'  # order_id = purchase order
SHOP_EVENT = 'woocommerce.salesorder_created'  # id = sales order (inventree-woocommerce plugin)
QUOTE_RECEIVED_EVENT = 'woocommerce.quote_received'  # id = quote request
QUOTE_ACCEPTED_EVENT = 'woocommerce.quote_accepted'  # id = quote request, sales_order

SYNC_DIRECTIONS = [
    ('both', 'Two-way (export and import)'),
    ('export', 'Export only (InvenTree → Xero)'),
    ('import', 'Import only (Xero → InvenTree)'),
    ('off', 'Off'),
]


class XeroPlugin(AppMixin, EventMixin, ScheduleMixin, SettingsMixin, UrlsMixin, UserInterfaceMixin, InvenTreePlugin):
    """Sync InvenTree orders, bills, contacts and items with Xero."""

    NAME = 'Xero'
    SLUG = 'xero'
    TITLE = 'Xero Integration'
    DESCRIPTION = 'Sync purchase orders, bills, sales orders, contacts and items between InvenTree and Xero'
    VERSION = PLUGIN_VERSION
    AUTHOR = 'Tyler Brannigan / OMG Harness'
    WEBSITE = 'https://github.com/SXSLYDA/inventree-xero'
    LICENSE = 'MIT'
    MIN_VERSION = '1.6.0'

    ADMIN_SOURCE = 'xero_ui.js:renderXeroAdmin'

    # Ticks every 5 minutes; the real interval is the SYNC_INTERVAL_MINUTES setting
    SCHEDULED_TASKS = {
        'xero_sync': {
            'func': 'run_scheduled_sync',
            'schedule': 'I',
            'minutes': 5,
        },
    }

    SETTINGS = {
        # ---------------------------------------------------------------- safety (read these first)
        'WRITE_MODE': {
            'name': 'Write mode',
            'description': ('Dry run: work out and log everything that would be sent, write nothing. '
                            'Live: write to Xero. Paused: the kill switch - nothing is sent or changed'),
            'choices': [('dry_run', 'Dry run (nothing written)'), ('live', 'Live'), ('paused', 'Paused (kill switch)')],
            'default': 'dry_run',
        },
        'AUTO_APPROVE': {
            'name': 'Let InvenTree approve documents',
            'description': ('Off (recommended): purchase orders, invoices and bills always go to Xero as Draft and a '
                            'person approves them in Xero. On: the approve-when settings below take effect'),
            'validator': bool,
            'default': False,
        },
        'ALLOW_DELETE_VOID': {
            'name': 'Allow delete / void',
            'description': ('Off (recommended): cancelling in InvenTree never deletes or voids a Xero document - '
                            'it is held for you to deal with in Xero'),
            'validator': bool,
            'default': False,
        },
        'IMPORT_NEW_DOCUMENTS': {
            'name': 'Import new Xero documents',
            'description': ('Create InvenTree orders from purchase orders / invoices / quotes entered in Xero. '
                            'Off: only the status of linked documents comes back from Xero'),
            'validator': bool,
            'default': False,
        },
        'CONTACT_MATCH': {
            'name': 'Contact matching',
            'description': ('Strict: link automatically only on an exact name with no conflicting email or ABN; '
                            'anything uncertain (duplicates, near-matches) is held for you to link by hand'),
            'choices': [('strict', 'Strict (recommended)'), ('name', 'Exact name only')],
            'default': 'strict',
        },
        'REQUIRE_TAX_TYPE': {
            'name': 'Require tax types',
            'description': 'Hold documents until the purchase and sales tax types are set (so GST is never guessed)',
            'validator': bool,
            'default': True,
        },
        'CHECK_TOTALS': {
            'name': 'Check totals',
            'description': 'Hold a document when its Xero lines don\'t add up to the InvenTree order total',
            'validator': bool,
            'default': True,
        },
        'ALLOW_ZERO_PRICE': {
            'name': 'Allow lines with no price',
            'description': 'Off: a document with any unpriced line is held',
            'validator': bool,
            'default': False,
        },
        'ALERT_EMAILS': {
            'name': 'Alert emails',
            'description': 'Who is emailed about errors and held documents (comma-separated). Blank = superusers',
            'default': '',
        },
        'AUDIT_RETENTION_DAYS': {
            'name': 'Audit retention (days)',
            'description': 'How long the record of every write to Xero is kept',
            'validator': [int, MinValueValidator(30)],
            'default': 365,
        },
        # ---------------------------------------------------------------- connection
        'AUTH_MODE': {
            'name': 'Connection type',
            'description': 'Standard OAuth app (Connect button) or a Xero Custom Connection (machine-to-machine)',
            'choices': [('oauth', 'OAuth 2.0 web app'), ('custom', 'Custom Connection')],
            'default': 'oauth',
        },
        'CLIENT_ID': {
            'name': 'Client ID',
            'description': 'From your app at developer.xero.com → My Apps',
            'default': '',
        },
        'CLIENT_SECRET': {
            'name': 'Client secret',
            'description': 'From your app at developer.xero.com → My Apps',
            'default': '',
            'protected': True,
        },
        'SCOPES': {
            'name': 'OAuth scopes',
            'description': ('Blank (recommended): only the Xero permissions the features you switched on need. '
                            'Reconnect after changing features that need more'),
            'default': '',
        },
        'BASE_URL': {
            'name': 'InvenTree public URL',
            'description': 'Used for the OAuth redirect URI, e.g. https://inventree.example.com',
            'default': '',
        },
        'TENANT_ID': {
            'name': 'Xero organisation (tenant) ID',
            'description': 'Leave blank to use the first organisation authorised',
            'default': '',
        },
        # ---------------------------------------------------------------- schedule
        'ENABLE_SCHEDULED': {
            'name': 'Scheduled sync',
            'description': 'Run the sync automatically in the background worker',
            'validator': bool,
            'default': True,
        },
        'SYNC_INTERVAL_MINUTES': {
            'name': 'Sync interval (minutes)',
            'description': 'How often the scheduled sync runs (minimum 5)',
            'validator': [int, MinValueValidator(5)],
            'default': 15,
        },
        'PUSH_ON_STATUS_CHANGE': {
            'name': 'Push on status change',
            'description': 'Send an order to Xero as soon as it is placed / issued / shipped / completed / cancelled',
            'validator': bool,
            'default': True,
        },
        # ---------------------------------------------------------------- purchase orders
        'PO_SYNC_DIRECTION': {
            'name': 'Purchase orders',
            'description': 'InvenTree purchase orders ↔ Xero purchase orders',
            'choices': SYNC_DIRECTIONS,
            'default': 'both',
        },
        'PO_EXPORT_STATUSES': {
            'name': 'PO statuses to export',
            'description': 'Comma-separated: pending, placed, on_hold, complete',
            'default': 'placed,on_hold,complete',
        },
        'PO_PLACED_XERO_STATUS': {
            'name': 'Xero status for placed POs',
            'description': 'Status for the Xero PO once the InvenTree PO is placed (only with "Let InvenTree approve")',
            'choices': [('DRAFT', 'Draft'), ('SUBMITTED', 'Awaiting approval'), ('AUTHORISED', 'Approved')],
            'default': 'AUTHORISED',
        },
        'PO_USE_INVENTREE_REFERENCE': {
            'name': 'Use InvenTree PO number in Xero',
            'description': 'Set the Xero purchase order number to the InvenTree reference',
            'validator': bool,
            'default': True,
        },
        'PO_IMPORT_STATUSES': {
            'name': 'Xero PO statuses to import',
            'description': 'New Xero POs in these statuses are created in InvenTree (DRAFT, SUBMITTED, AUTHORISED, BILLED)',
            'default': 'SUBMITTED,AUTHORISED',
        },
        # ---------------------------------------------------------------- bills
        'BILLS_MODE': {
            'name': 'Create bills',
            'description': 'Create a Xero bill (accounts payable) from received purchase order lines',
            'choices': [
                ('off', 'Off'),
                ('on_receipt', 'As stock is received (draft bill kept up to date)'),
                ('on_complete', 'When the purchase order is completed'),
            ],
            'default': 'off',
        },
        'BILL_STATUS': {
            'name': 'Bill status when PO complete',
            'description': 'Only with "Let InvenTree approve". Draft lets you add the supplier invoice no. and freight first',
            'choices': [('DRAFT', 'Draft'), ('SUBMITTED', 'Awaiting approval'), ('AUTHORISED', 'Approved')],
            'default': 'DRAFT',
        },
        'BILL_DUE_DAYS': {
            'name': 'Bill due days',
            'description': 'Bill due date = bill date + this many days',
            'validator': [int, MinValueValidator(0)],
            'default': 30,
        },
        'MARK_PO_BILLED': {
            'name': 'Mark Xero PO as billed',
            'description': 'When the PO is complete and its bill exists, set the Xero purchase order to Billed',
            'validator': bool,
            'default': True,
        },
        'LINK_XERO_BILLS': {
            'name': 'Link bills entered in Xero',
            'description': 'Link a bill created in Xero to the InvenTree PO whose number appears in its reference',
            'validator': bool,
            'default': True,
        },
        # ---------------------------------------------------------------- sales
        'SO_SYNC_DIRECTION': {
            'name': 'Sales orders',
            'description': 'InvenTree sales orders ↔ Xero sales invoices or quotes',
            'choices': SYNC_DIRECTIONS,
            'default': 'both',
        },
        'SO_XERO_DOCUMENT': {
            'name': 'Xero document for sales orders',
            'description': 'Xero has no sales orders - use sales invoices or quotes (web shop orders are always invoices)',
            'choices': [('INVOICE', 'Sales invoice'), ('QUOTE', 'Quote')],
            'default': 'INVOICE',
        },
        'SO_EXPORT_STATUSES': {
            'name': 'SO statuses to export',
            'description': 'Comma-separated: pending, in_progress, on_hold, shipped, complete',
            'default': 'in_progress,shipped,complete',
        },
        'SO_AUTHORISE_ON': {
            'name': 'Approve invoice when',
            'description': 'Only with "Let InvenTree approve": invoices stay Draft until the order reaches this stage',
            'choices': [
                ('never', 'Never (approve in Xero)'),
                ('issued', 'Order issued'),
                ('shipped', 'Order shipped'),
                ('complete', 'Order complete'),
            ],
            'default': 'never',
        },
        'SO_DUE_DAYS': {
            'name': 'Invoice due days',
            'description': 'Due date when the sales order has no target date',
            'validator': [int, MinValueValidator(0)],
            'default': 30,
        },
        'SO_USE_INVENTREE_REFERENCE': {
            'name': 'Use InvenTree SO number in Xero',
            'description': 'Use the SO reference as the invoice/quote number (off: Xero numbering, SO ref in Reference)',
            'validator': bool,
            'default': False,
        },
        'INVOICE_IMPORT_STATUSES': {
            'name': 'Xero invoice statuses to import',
            'description': 'New Xero sales invoices in these statuses become InvenTree sales orders',
            'default': 'AUTHORISED,PAID',
        },
        'QUOTE_IMPORT_STATUSES': {
            'name': 'Xero quote statuses to import',
            'description': 'New Xero quotes in these statuses become InvenTree sales orders (quote mode)',
            'default': 'ACCEPTED',
        },
        'SO_ISSUE_ON_IMPORT': {
            'name': 'Issue imported sales orders',
            'description': 'Issue the InvenTree SO when its Xero invoice is approved / quote accepted',
            'validator': bool,
            'default': True,
        },
        # ---------------------------------------------------------------- what InvenTree holds
        'NON_PART_LINES': {
            'name': 'Xero lines that are not parts',
            'description': 'Labour, freight, interest etc. on Xero documents',
            'choices': [
                ('xero', 'Keep in Xero only (InvenTree holds parts only)'),
                ('extra', 'Also import as InvenTree extra lines'),
            ],
            'default': 'xero',
        },
        'IMPORT_SKIP_NO_PARTS': {
            'name': 'Skip Xero documents without parts',
            'description': 'Do not create InvenTree orders for Xero documents with no part lines (labour, interest...)',
            'validator': bool,
            'default': True,
        },
        'IMPORT_UPDATE_LINES': {
            'name': 'Update lines from Xero',
            'description': 'Refresh part lines of Xero-originated orders that are still pending in InvenTree',
            'validator': bool,
            'default': True,
        },
        # ---------------------------------------------------------------- contacts
        'CONTACT_SYNC': {
            'name': 'Contacts',
            'description': 'How suppliers and customers are linked to Xero contacts',
            'choices': [
                ('link', 'Link existing contacts only'),
                ('create', 'Link, or create when missing'),
                ('create_update', 'Link or create, and push InvenTree detail changes'),
            ],
            'default': 'create',
        },
        'AUTO_CREATE_COMPANIES': {
            'name': 'Create InvenTree companies',
            'description': 'Create a supplier/customer when an imported Xero contact has no match',
            'validator': bool,
            'default': True,
        },
        'BALANCE_SYNC': {
            'name': 'Import account balances',
            'description': 'Store each linked company\'s outstanding/overdue Xero balance (used for shop credit checks)',
            'validator': bool,
            'default': True,
        },
        'BALANCE_INTERVAL_MINUTES': {
            'name': 'Balance refresh (minutes)',
            'description': 'How often balances are refreshed (minimum 15)',
            'validator': [int, MinValueValidator(15)],
            'default': 60,
        },
        'BALANCE_PARAMETER': {
            'name': 'Balance parameter',
            'description': 'Also write the customer\'s outstanding balance to this company parameter (blank = off)',
            'default': 'Xero Outstanding',
        },
        # ---------------------------------------------------------------- items
        'ITEM_CODE_SOURCE': {
            'name': 'Xero item code from',
            'description': 'Which InvenTree field matches the Xero Item code (max 30 characters)',
            'choices': [('name', 'Part name'), ('ipn', 'Part IPN'), ('sku', 'Supplier SKU (purchase lines)')],
            'default': 'name',
        },
        'CREATE_XERO_ITEMS': {
            'name': 'Create Xero items',
            'description': 'Create missing Xero Items for exported lines (off: lines are sent without an item code)',
            'validator': bool,
            'default': False,
        },
        'AUTO_CREATE_SUPPLIER_PARTS': {
            'name': 'Create supplier parts on import',
            'description': 'Link an imported PO line to a part by creating the missing supplier part',
            'validator': bool,
            'default': False,
        },
        # ---------------------------------------------------------------- web shop
        'SHOP_INTEGRATION': {
            'name': 'Web shop integration',
            'description': 'With the inventree-woocommerce plugin: invoice web orders as soon as they arrive',
            'validator': bool,
            'default': True,
        },
        'SHOP_AUTHORISE_PAID': {
            'name': 'Approve paid web order invoices',
            'description': 'Only with "Let InvenTree approve": web orders paid online get an approved invoice straight away',
            'validator': bool,
            'default': True,
        },
        'SHOP_ORDER_DOCUMENT': {
            'name': 'Xero document for web orders',
            'description': 'Paid / charged-to-account web orders are sales, so normally invoices',
            'choices': [('invoice', 'Sales invoice'), ('follow', 'Same as "Xero document for sales orders"')],
            'default': 'invoice',
        },
        'SHOP_QUOTES': {
            'name': 'Web shop quote requests',
            'description': ('Price in Xero: each request becomes a draft Xero quote; price it there (parts, labour, '
                            'freight - any line), mark it Sent and the shop sends the customer Xero\'s PDF. The shop\'s '
                            'own pricing is locked meanwhile (back to InvenTree pricing in a dry run or if '
                            'disconnected). The other choices price in InvenTree and copy the quote to Xero'),
            'choices': [
                ('xero', 'Price and send in Xero (Xero is the quote)'),
                ('draft', 'Priced in InvenTree, draft in Xero as soon as a request arrives'),
                ('sent', 'Priced in InvenTree, into Xero once it is sent to the customer'),
                ('off', 'Off'),
            ],
            'default': 'xero',
        },
        'SHOP_QUOTE_XERO_EDITS': {
            'name': 'Xero quote edits go to the shop',
            'description': ('Only when priced in InvenTree (always on with "Price and send in Xero"): line changes in '
                            'Xero update the shop quote; marking it Sent sends it to the customer; Draft reopens it; '
                            'Declined declines it (Summary = reason)'),
            'validator': bool,
            'default': True,
        },
        'SHOP_QUOTE_PARTIAL_ACCEPT': {
            'name': 'Customers can accept part of a Xero quote',
            'description': ('Only with "Price and send in Xero". Off (recommended): customers accept the whole quote, '
                            'so the order always matches the Xero quote'),
            'validator': bool,
            'default': False,
        },
        'SHOP_QUOTE_AUTO_RESEND': {
            'name': 'Re-send revised quotes automatically',
            'description': ('Off (recommended): a sent quote you revise in Xero goes back to Draft; marking it Sent '
                            'again is what sends the revision to the customer'),
            'validator': bool,
            'default': False,
        },
        'SHOP_QUOTE_DECLINE_REASON': {
            'name': 'Default decline reason',
            'description': 'Sent to the customer when a quote is declined in Xero without a Summary (blank = require one)',
            'default': '',
        },
        'SHOP_PAYMENT_ACCOUNT': {
            'name': 'Web shop payment account',
            'description': ('Only with "Let InvenTree approve". Xero account to record online payments against (blank = off, '
                            'recommended - let your Stripe/bank feed reconcile payments)'),
            'default': '',
        },
        # ---------------------------------------------------------------- accounting
        'PURCHASE_ACCOUNT_CODE': {
            'name': 'Purchase account code',
            'description': 'Xero account for purchase order and bill lines',
            'default': '300',
        },
        'PURCHASE_TAX_TYPE': {
            'name': 'Purchase tax type',
            'description': 'Xero tax type for purchase lines, e.g. INPUT (AU GST on expenses). Blank = account default',
            'default': '',
        },
        'SALES_ACCOUNT_CODE': {
            'name': 'Sales account code',
            'description': 'Xero account for invoice and quote lines',
            'default': '200',
        },
        'SALES_TAX_TYPE': {
            'name': 'Sales tax type',
            'description': 'Xero tax type for sales lines, e.g. OUTPUT (AU GST on income). Blank = account default',
            'default': '',
        },
        'LINE_AMOUNT_TYPES': {
            'name': 'Prices are',
            'description': 'How InvenTree line prices relate to tax',
            'choices': [('Exclusive', 'Tax exclusive'), ('Inclusive', 'Tax inclusive'), ('NoTax', 'No tax')],
            'default': 'Exclusive',
        },
        'SEND_CURRENCY': {
            'name': 'Send order currency',
            'description': 'Send the order currency (needs Xero multi-currency). Off = Xero base currency',
            'validator': bool,
            'default': False,
        },
        # ---------------------------------------------------------------- housekeeping
        'IMPORT_SINCE_DAYS': {
            'name': 'First import look-back (days)',
            'description': 'On the first run, import Xero documents modified in this many days',
            'validator': [int, MinValueValidator(1)],
            'default': 30,
        },
        'EXPORT_LOOKBACK_DAYS': {
            'name': 'Export look-back (days)',
            'description': 'Only orders created within this many days are exported (linked orders always sync)',
            'validator': [int, MinValueValidator(1)],
            'default': 180,
        },
        'SET_ORDER_LINK': {
            'name': 'Set order link',
            'description': 'Put the Xero document URL in the order Link field (if empty)',
            'validator': bool,
            'default': True,
        },
        'TAG_DESCRIPTION': {
            'name': 'Tag description with Xero number',
            'description': 'Append [Xero INV-0001] to the order description so InvenTree search finds it',
            'validator': bool,
            'default': True,
        },
        'LOG_RETENTION_DAYS': {
            'name': 'Log retention (days)',
            'description': 'Sync log entries older than this are deleted',
            'validator': [int, MinValueValidator(1)],
            'default': 60,
        },
    }

    # ------------------------------------------------------------------ schedule
    def run_scheduled_sync(self, *args, **kwargs):
        """Called every 5 min by the background worker; syncs when the interval is due."""
        from InvenTree.helpers import str2bool

        from .models import XeroConnection
        from .sync import XeroSync

        if not str2bool(self.get_setting('ENABLE_SCHEDULED')) or self.get_setting('WRITE_MODE') == 'paused':
            return
        conn = XeroConnection.get_solo()
        if not conn.is_connected:
            return
        try:
            interval = max(int(self.get_setting('SYNC_INTERVAL_MINUTES')), 5)
        except (TypeError, ValueError):
            interval = 15
        due = conn.last_sync_started + timedelta(minutes=interval) if conn.last_sync_started else None
        if due and due > timezone.now() + timedelta(seconds=60):
            return
        XeroSync(self).run(triggered_by='schedule')

    def run_selftest_task(self, *args, **kwargs):
        """Background-task target for "Run live self-test" (Demo Company only)."""
        from .selftest import run_selftest

        return run_selftest(self, confirm_demo=True).as_dict()

    def run_manual_sync(self, direction='all', *args, **kwargs):
        """Background-task target for the 'Sync now' buttons."""
        from .sync import XeroSync

        return XeroSync(self).run(direction=direction, triggered_by='manual')

    # ------------------------------------------------------------------ events
    def wants_process_event(self, event):
        from InvenTree.helpers import str2bool

        if event == SHOP_EVENT:
            return str2bool(self.get_setting('SHOP_INTEGRATION'))
        if event in (QUOTE_RECEIVED_EVENT, QUOTE_ACCEPTED_EVENT):
            return str2bool(self.get_setting('SHOP_INTEGRATION')) and self.get_setting('SHOP_QUOTES') != 'off'
        if event == RECEIPT_EVENT:
            return self.get_setting('BILLS_MODE') == 'on_receipt'
        return event in ORDER_EVENTS and str2bool(self.get_setting('PUSH_ON_STATUS_CHANGE'))

    def process_event(self, event, *args, **kwargs):
        from .models import XeroConnection
        from .sync import XeroSync

        if not XeroConnection.get_solo().is_connected or self.get_setting('WRITE_MODE') == 'paused':
            return
        sync = XeroSync(self)

        if event == RECEIPT_EVENT:
            if kwargs.get('order_id'):
                sync.export_single('bill', kwargs['order_id'])
            return

        if event == SHOP_EVENT:
            if kwargs.get('id'):
                sync.export_single('so', kwargs['id'])
            return

        if event in (QUOTE_RECEIVED_EVENT, QUOTE_ACCEPTED_EVENT):
            from .shop_quotes import shop_models

            models = shop_models()
            quote = models[0].objects.filter(pk=kwargs.get('id')).first() if models else None
            if quote is None:
                return
            sync.client.ensure_token()
            if event == QUOTE_ACCEPTED_EVENT and kwargs.get('sales_order'):
                sync.shop_quotes.link_sales_order(quote.pk, kwargs['sales_order'])
            else:
                sync.shop_quotes.safe(sync.shop_quotes.export_quote, quote)
            return

        kind = ORDER_EVENTS.get(event)
        pk = kwargs.get('id')
        if kind == 'shipment':
            from order.models import SalesOrderShipment

            shipment = SalesOrderShipment.objects.filter(pk=pk).first()
            if not shipment:
                return
            kind, pk = 'so', shipment.order_id
        if pk is None:
            return
        sync.export_single(kind, pk)
        if event == 'purchaseorder.completed' and self.get_setting('BILLS_MODE') != 'off':
            sync.export_single('bill', pk)

    # ------------------------------------------------------------------ web shop quote pricing
    # The inventree-woocommerce plugin looks for these hooks on active plugins (its providers.py)
    def woocommerce_quote_provider(self):
        from .shop_quotes import provider_details

        return provider_details(self)

    def woocommerce_quote_info(self, quote):
        from .shop_quotes import provider_info

        return provider_info(self, quote)

    def woocommerce_quote_pdf(self, quote):
        from .shop_quotes import provider_pdf

        return provider_pdf(self, quote)

    # ------------------------------------------------------------------ URLs
    def setup_urls(self):
        from django.urls import path

        from . import views

        return [
            path('oauth/connect/', views.oauth_connect, name='oauth-connect'),
            path('oauth/callback/', views.oauth_callback, name='oauth-callback'),
            path('api/status/', views.StatusView.as_view(), name='api-status'),
            path('api/connect-custom/', views.ConnectCustomView.as_view(), name='api-connect-custom'),
            path('api/disconnect/', views.DisconnectView.as_view(), name='api-disconnect'),
            path('api/test/', views.TestConnectionView.as_view(), name='api-test'),
            path('api/sync/', views.SyncNowView.as_view(), name='api-sync'),
            path('api/log/', views.LogView.as_view(), name='api-log'),
            path('api/search/', views.SearchView.as_view(), name='api-search'),
            path('api/order/<str:kind>/<int:pk>/', views.OrderXeroView.as_view(), name='api-order'),
            path('api/order/<str:kind>/<int:pk>/<str:action>/', views.OrderXeroView.as_view(), name='api-order-action'),
            path('api/company/<int:pk>/', views.CompanyXeroView.as_view(), name='api-company'),
            path('api/part/<int:pk>/', views.PartXeroView.as_view(), name='api-part'),
            path('api/quotes/', views.ShopQuotesView.as_view(), name='api-quotes'),
            path('api/mode/', views.WriteModeView.as_view(), name='api-mode'),
            path('api/check/', views.CheckView.as_view(), name='api-check'),
            path('api/selftest/', views.SelfTestView.as_view(), name='api-selftest'),
            path('api/audit/', views.AuditView.as_view(), name='api-audit'),
            path('api/company/<int:pk>/candidates/', views.CompanyCandidatesView.as_view(), name='api-company-candidates'),
            path('api/quotes/<int:pk>/<str:action>/', views.ShopQuotesView.as_view(), name='api-quote-action'),
        ]

    # ------------------------------------------------------------------ UI
    def get_admin_context(self) -> dict:
        return {'api': f'{self.base_url}api/'}

    def get_ui_panels(self, request, context, **kwargs):
        target = (context or {}).get('target_model')
        target_id = (context or {}).get('target_id')
        kinds = {'purchaseorder': 'po', 'salesorder': 'so', 'company': 'company', 'part': 'part'}
        if target not in kinds or not target_id:
            return []
        return [{
            'key': 'xero',
            'title': 'Xero',
            'description': 'Xero link and sync status',
            'icon': 'ti:file-invoice:outline',
            'source': self.plugin_static_file('xero_ui.js:renderXeroPanel'),
            'context': {'kind': kinds[target], 'pk': target_id, 'api': f'{self.base_url}api/'},
        }]

    def get_ui_dashboard_items(self, request, context, **kwargs):
        return [{
            'key': 'xero-sync',
            'title': 'Xero Orders',
            'description': 'Search Xero-linked purchase and sales orders and bills, sync status',
            'icon': 'ti:file-invoice:outline',
            'source': self.plugin_static_file('xero_ui.js:renderXeroDashboard'),
            'context': {'api': f'{self.base_url}api/'},
        }]
