"""Read-only configuration check — safe to run against your real Xero organisation.

It reads from Xero and InvenTree, writes nothing, and reports whether the plugin
is set up correctly for *this* organisation:

- connection, organisation, demo or real, lock dates
- OAuth permissions granted vs. needed by the switched-on features
- the account codes and tax types in the settings exist, are active and suit their use
- the web shop payment account (if set) can take payments
- base currency vs InvenTree's default currency
- whether Xero's contact list includes balances (or the slower per-contact path is used)
- write mode, approval and delete settings, alert recipients, background worker

Run it from the plugin settings panel ("Check configuration") or:
    python manage.py xero_check
"""

from .xero_client import XeroError, parse_xero_date

PURCHASE_ACCOUNT_TYPES = {'EXPENSE', 'DIRECTCOSTS', 'OVERHEADS', 'INVENTORY', 'CURRENT', 'FIXED'}
SALES_ACCOUNT_TYPES = {'REVENUE', 'SALES', 'OTHERINCOME'}


class Report:
    """A list of results: ok / warn / fail / info, each with a plain-English message."""

    def __init__(self, title):
        self.title = title
        self.items = []

    def add(self, level, name, message):
        self.items.append({'level': level, 'name': name, 'message': message})

    def ok(self, name, message):
        self.add('ok', name, message)

    def warn(self, name, message):
        self.add('warn', name, message)

    def fail(self, name, message):
        self.add('fail', name, message)

    def info(self, name, message):
        self.add('info', name, message)

    @property
    def passed(self):
        return not any(i['level'] == 'fail' for i in self.items)

    def counts(self):
        out = {'ok': 0, 'warn': 0, 'fail': 0, 'info': 0}
        for item in self.items:
            out[item['level']] += 1
        return out

    def as_dict(self):
        return {'title': self.title, 'passed': self.passed, 'counts': self.counts(), 'items': self.items,
                'text': self.as_text()}

    def as_text(self):
        marks = {'ok': 'PASS', 'warn': 'WARN', 'fail': 'FAIL', 'info': 'INFO'}
        c = self.counts()
        lines = [self.title, f"{c['ok']} passed, {c['warn']} warnings, {c['fail']} failed", '']
        lines += [f"[{marks[i['level']]}] {i['name']}: {i['message']}" for i in self.items]
        return '\n'.join(lines)


def check_configuration(plugin, client=None):
    """Run every read-only check. Never raises; problems become report lines."""
    from InvenTree.helpers import str2bool

    from .xero_client import XeroClient

    report = Report('inventree-xero configuration check')
    setting = plugin.get_setting

    # ---------------------------------------------------------------- plugin settings
    mode = setting('WRITE_MODE') or 'dry_run'
    report.info('Write mode', {'dry_run': 'Dry run - nothing is written to Xero',
                               'live': 'LIVE - writing to Xero', 'paused': 'Paused (kill switch)'}[mode])
    if str2bool(setting('AUTO_APPROVE')):
        report.warn('Approvals', 'InvenTree may approve documents in Xero ("Let InvenTree approve" is on)')
    else:
        report.ok('Approvals', 'Drafts only - documents are approved by a person in Xero')
    if str2bool(setting('ALLOW_DELETE_VOID')):
        report.warn('Delete / void', 'InvenTree may delete or void Xero documents ("Allow delete / void" is on)')
    else:
        report.ok('Delete / void', 'Never automatic')

    from .alerts import recipients

    to = recipients(plugin)
    if to:
        report.ok('Alert emails', ', '.join(to))
    else:
        report.warn('Alert emails', 'No recipients - set "Alert emails" or give a superuser an email address')
    try:
        from InvenTree.helpers_email import is_email_configured

        if is_email_configured():
            report.ok('Email', 'InvenTree can send email')
        else:
            report.warn('Email', 'InvenTree email is not configured - alerts will not be delivered')
    except Exception:  # noqa: BLE001
        report.info('Email', 'Could not check InvenTree email settings')
    try:
        from InvenTree.status import is_worker_running

        if is_worker_running():
            report.ok('Background worker', 'Running - the schedule and pushes will happen')
        else:
            report.fail('Background worker', 'Not running - nothing will sync until it is started')
    except Exception:  # noqa: BLE001
        report.info('Background worker', 'Could not check the background worker')

    # ---------------------------------------------------------------- connection
    try:
        client = client or XeroClient(plugin)
    except Exception as exc:  # noqa: BLE001
        report.fail('Connection', f'Could not load the connection: {exc}')
        return report
    if not client.conn.is_connected:
        report.fail('Connection', 'Not connected to Xero - use "Connect to Xero" on the settings panel')
        return report
    try:
        client.ensure_token()
        org = client.load_organisation()
    except Exception as exc:  # noqa: BLE001
        report.fail('Connection', f'Could not reach Xero: {exc}')
        return report

    demo = bool(org.get('IsDemoCompany')) or org.get('Class') == 'DEMO'
    report.ok('Connection', f'Connected to "{org.get("Name")}" ({org.get("CountryCode")}, '
                            f'{"DEMO COMPANY" if demo else "real organisation"}, {org.get("Class") or "?"})')
    if demo and mode == 'live':
        report.info('Organisation', 'This is a Demo Company - fine for testing; reconnect your real organisation to go live')

    lock = parse_xero_date(org.get('PeriodLockDate') or '')
    if lock:
        report.info('Lock date', f'Xero rejects documents dated on or before {lock.date():%d %b %Y}')

    # ---------------------------------------------------------------- permissions
    granted = set((client.conn.scopes or '').split())
    needed = client.scopes(for_client_credentials=True).split()
    missing = client.missing_scopes()
    if missing:
        report.fail('Permissions', f'Missing {" ".join(missing)} - reconnect to Xero to grant them')
    else:
        report.ok('Permissions', f'Granted what the enabled features need ({" ".join(needed)})')
    extra = sorted(s for s in granted - set(needed) if s.startswith('accounting.') and s + '.read' not in needed)
    if extra:
        report.info('Permissions', f'Also granted (not needed by current settings): {" ".join(extra)}')

    # ---------------------------------------------------------------- currency
    try:
        from common.currency import currency_code_default

        inventree_currency = currency_code_default()
        if org.get('BaseCurrency') and inventree_currency != org.get('BaseCurrency'):
            report.warn('Currency', f'InvenTree\'s default currency is {inventree_currency} but Xero\'s base currency is '
                                    f'{org.get("BaseCurrency")} - prices go to Xero in its base currency unless '
                                    f'"Send order currency" is on')
        else:
            report.ok('Currency', f'{inventree_currency} in both')
    except Exception:  # noqa: BLE001
        report.info('Currency', 'Could not compare currencies')

    # ---------------------------------------------------------------- accounts and tax
    try:
        accounts = {a.get('Code'): a for a in client.get('Accounts').get('Accounts') or [] if a.get('Code')}
        tax_rates = {t.get('TaxType'): t for t in client.get('TaxRates').get('TaxRates') or []}
    except XeroError as exc:
        report.fail('Accounts & tax', f'Could not read the chart of accounts / tax rates: {exc}')
        accounts, tax_rates = None, None

    if accounts is not None:
        _check_account(report, accounts, setting('PURCHASE_ACCOUNT_CODE'), 'Purchase account', PURCHASE_ACCOUNT_TYPES)
        _check_account(report, accounts, setting('SALES_ACCOUNT_CODE'), 'Sales account', SALES_ACCOUNT_TYPES)
        _check_tax(report, tax_rates, setting('PURCHASE_TAX_TYPE'), 'Purchase tax type', 'CanApplyToExpenses',
                   setting('LINE_AMOUNT_TYPES'), str2bool(setting('REQUIRE_TAX_TYPE')))
        _check_tax(report, tax_rates, setting('SALES_TAX_TYPE'), 'Sales tax type', 'CanApplyToRevenue',
                   setting('LINE_AMOUNT_TYPES'), str2bool(setting('REQUIRE_TAX_TYPE')))
        payment = (setting('SHOP_PAYMENT_ACCOUNT') or '').strip()
        if payment:
            acct = accounts.get(payment)
            if acct is None:
                report.fail('Shop payment account', f'Account {payment} does not exist in Xero')
            elif acct.get('Type') != 'BANK' and not acct.get('EnablePaymentsToAccount'):
                report.fail('Shop payment account', f'Account {payment} ({acct.get("Name")}) cannot take payments - '
                                                    f'use a bank/clearing account or enable payments to it in Xero')
            else:
                report.ok('Shop payment account', f'{payment} {acct.get("Name")}')

    # ---------------------------------------------------------------- contacts
    try:
        rows = client.get('Contacts', params={'page': 1, 'pageSize': 5}).get('Contacts') or []
        if not rows:
            report.info('Contact balances', 'No contacts in Xero yet')
        elif any('Balances' in r for r in rows):
            report.ok('Contact balances', 'Xero\'s contact list includes balances (one call per 50 customers)')
        else:
            report.info('Contact balances', 'The contact list has no balances - each customer\'s balance is read '
                                            'separately (slower, still within Xero\'s limits)')
    except XeroError as exc:
        report.fail('Contacts', f'Could not read contacts: {exc}')

    # ---------------------------------------------------------------- held work
    try:
        from .views import safety_info

        held = safety_info(plugin)['held']
        if held:
            report.warn('Held for review', f'{held} document(s) are held - see the Xero Orders widget, filter "Held"')
        else:
            report.ok('Held for review', 'Nothing held')
    except Exception:  # noqa: BLE001
        pass
    return report


def _check_account(report, accounts, code, label, suitable):
    code = (code or '').strip()
    if not code:
        report.fail(label, 'Not set - documents will be held until it is')
        return
    acct = accounts.get(code)
    if acct is None:
        report.fail(label, f'Account {code} does not exist in this Xero organisation')
    elif acct.get('Status') not in (None, 'ACTIVE'):
        report.fail(label, f'Account {code} ({acct.get("Name")}) is {acct.get("Status")}')
    elif acct.get('Type') not in suitable:
        report.warn(label, f'Account {code} ({acct.get("Name")}) is a {acct.get("Type")} account - check it suits this use')
    else:
        report.ok(label, f'{code} {acct.get("Name")} ({acct.get("Type")})')


def _check_tax(report, rates, tax_type, label, applies, line_amount_types, required):
    tax_type = (tax_type or '').strip()
    if not tax_type:
        if line_amount_types == 'NoTax':
            report.ok(label, 'Not needed (prices set to No tax)')
        elif required:
            report.fail(label, 'Not set - documents will be held until it is (AU: INPUT for purchases, OUTPUT for sales)')
        else:
            report.warn(label, 'Not set - each account\'s default tax rate will be used')
        return
    rate = rates.get(tax_type)
    if rate is None:
        report.fail(label, f'Tax type {tax_type} does not exist in this Xero organisation')
    elif rate.get('Status') not in (None, 'ACTIVE'):
        report.fail(label, f'Tax type {tax_type} ({rate.get("Name")}) is {rate.get("Status")}')
    elif rate.get(applies) is False:
        report.warn(label, f'Tax type {tax_type} ({rate.get("Name")}) is not normally used for this - check it')
    else:
        report.ok(label, f'{tax_type} {rate.get("Name")} ({rate.get("EffectiveRate", rate.get("DisplayTaxRate", "?"))}%)')
