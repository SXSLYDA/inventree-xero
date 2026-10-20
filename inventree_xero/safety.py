"""Safety rules applied before anything is written to Xero.

- Checks: a document is *held* (not sent, reason shown on the order and alerted)
  when a line has no price or quantity, the account code or tax type isn't set,
  or the lines don't add up to the InvenTree order total.
- Drafts only (default): InvenTree never approves anything in Xero. Approved,
  awaiting-approval and billed statuses are capped to Draft, and once a person
  approves a document in Xero, later InvenTree changes are held for a person
  instead of being written over it.
- No destructive actions (default): cancelling in InvenTree never deletes or
  voids a Xero document; it is held for a person to deal with in Xero.
"""

from .mapping import as_float

LEDGER_TYPES = ('po', 'invoice', 'bill')
APPROVED = {'AUTHORISED', 'SUBMITTED', 'BILLED', 'PAID'}


class Hold(Exception):
    """Not sent to Xero: needs a person. The message says why and what to do."""


class Safety:
    def __init__(self, sync):
        self.sync = sync

    # ------------------------------------------------------------------ settings
    @property
    def auto_approve(self):
        return self.sync.flag('AUTO_APPROVE')

    @property
    def allow_destructive(self):
        return self.sync.flag('ALLOW_DELETE_VOID')

    # ------------------------------------------------------------------ status rules
    def cap(self, doc_type, target):
        """Drafts only: InvenTree never approves a purchase order, invoice or bill."""
        if doc_type in LEDGER_TYPES and not self.auto_approve and target in ('AUTHORISED', 'SUBMITTED'):
            return 'DRAFT'
        return target

    def destructive(self, doc_type, number, action):
        """Deleting / voiding needs a person unless explicitly allowed."""
        if not self.allow_destructive:
            label = {'po': 'purchase order', 'invoice': 'invoice', 'bill': 'bill'}.get(doc_type, doc_type)
            raise Hold(f'Cancelled in InvenTree. Automatic delete/void is off - {action.lower()} Xero {label} '
                       f'{number or ""} in Xero yourself if it should go.'.replace('  ', ' '))

    def approved_in_xero(self, doc_type, current, payload):
        """A person approved it in Xero: don't write InvenTree line changes over it (drafts-only mode)."""
        if self.auto_approve or doc_type not in LEDGER_TYPES or 'LineItems' not in payload:
            return
        if current in APPROVED:
            raise Hold(f'Changed in InvenTree after it was approved in Xero ({current}). Not sent - update the '
                       f'Xero document yourself, or set it back to Draft in Xero and the change will follow.')

    # ------------------------------------------------------------------ checks
    def problems(self, kind, order, payload, compare_total=True, placeholder=False):
        """Everything wrong with a payload, as readable sentences (empty = OK to send)."""
        problems = []
        lines = payload.get('LineItems')
        if lines is None:
            return problems
        if not lines:
            return ['no lines to send']
        account_kind = 'purchase' if kind in ('po', 'bill') else 'sales'
        details = self.sync.account_details(account_kind)
        if not details.get('AccountCode'):
            problems.append(f'the {account_kind} account code is not set in the plugin settings')
        if (self.sync.flag('REQUIRE_TAX_TYPE') and (self.sync.setting('LINE_AMOUNT_TYPES') or 'Exclusive') != 'NoTax'
                and not details.get('TaxType')):
            problems.append(f'the {account_kind} tax type is not set in the plugin settings (e.g. '
                            f'{"INPUT" if account_kind == "purchase" else "OUTPUT"} for Australian GST)')
        allow_zero = self.sync.flag('ALLOW_ZERO_PRICE') or placeholder
        for n, line in enumerate(lines, start=1):
            label = (line.get('Description') or line.get('ItemCode') or f'line {n}')[:60]
            if as_float(line.get('Quantity')) <= 0:
                problems.append(f'"{label}" has no quantity')
            if not allow_zero and as_float(line.get('UnitAmount')) <= 0:
                problems.append(f'"{label}" has no price')
        if compare_total and self.sync.flag('CHECK_TOTALS'):
            mismatch = self.total_mismatch(order, lines)
            if mismatch:
                problems.append(mismatch)
        return problems

    @staticmethod
    def line_total(lines):
        total = 0.0
        for line in lines:
            total += as_float(line.get('Quantity')) * as_float(line.get('UnitAmount')) * (
                1 - as_float(line.get('DiscountRate')) / 100)
        return round(total, 2)

    def total_mismatch(self, order, lines):
        """The Xero lines must add up to InvenTree's own order total (same currency)."""
        if order is None or not hasattr(order, 'calculate_total_price'):
            return None
        total = order.calculate_total_price(target_currency=order.currency)  # fresh, the way InvenTree adds it up
        amount = as_float(getattr(total, 'amount', total))
        if amount == 0:
            return None  # no total to compare (e.g. no exchange rate); unpriced lines are caught separately
        sent = self.line_total(lines)
        if abs(sent - round(amount, 2)) > 0.01 + 0.005 * len(lines):
            currency = getattr(total, 'currency', '')
            return f'the lines add up to {sent:.2f} but the InvenTree order total is {amount:.2f} {currency}'.strip()
        return None

    def check(self, kind, order, payload, **kwargs):
        problems = self.problems(kind, order, payload, **kwargs)
        if problems:
            raise Hold('Not sent to Xero: ' + '; '.join(problems) + '.')
