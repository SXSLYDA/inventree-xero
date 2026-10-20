"""Unit tests for the safety rules (no InvenTree needed): pytest tests/test_safety.py"""

from types import SimpleNamespace

import pytest

from inventree_xero.crypto import decrypt, encrypt, is_encrypted
from inventree_xero.mapping import contact_conflicts, normalise_name
from inventree_xero.safety import Hold, Safety
from inventree_xero.xero_client import idempotency_key, required_scopes


def make_sync(**settings):
    values = {'AUTO_APPROVE': False, 'ALLOW_DELETE_VOID': False, 'REQUIRE_TAX_TYPE': True, 'CHECK_TOTALS': True,
              'ALLOW_ZERO_PRICE': False, 'LINE_AMOUNT_TYPES': 'Exclusive'}
    values.update(settings)
    account = {'purchase': {'AccountCode': '300', 'TaxType': 'INPUT'}, 'sales': {'AccountCode': '200', 'TaxType': 'OUTPUT'}}
    if values.pop('NO_TAX', False):
        account = {k: {'AccountCode': v['AccountCode']} for k, v in account.items()}
    return SimpleNamespace(flag=lambda k: bool(values.get(k)), setting=lambda k: values.get(k),
                           account_details=lambda kind: dict(account[kind]))


def order_total(amount):
    return SimpleNamespace(currency='AUD', calculate_total_price=lambda target_currency=None: SimpleNamespace(
        amount=amount, currency='AUD'))


LINES = [{'Description': 'Plug', 'Quantity': 10, 'UnitAmount': 2.0, 'DiscountRate': 10},
         {'Description': 'Freight', 'Quantity': 1, 'UnitAmount': 12.0}]


def test_drafts_only_caps_approval():
    safety = Safety(make_sync())
    assert safety.cap('po', 'AUTHORISED') == 'DRAFT'
    assert safety.cap('invoice', 'SUBMITTED') == 'DRAFT'
    assert safety.cap('quote', 'SENT') == 'SENT', 'quotes carry no ledger entries'
    assert Safety(make_sync(AUTO_APPROVE=True)).cap('bill', 'AUTHORISED') == 'AUTHORISED'


def test_delete_void_needs_permission():
    with pytest.raises(Hold, match='delete/void is off'):
        Safety(make_sync()).destructive('invoice', 'INV-1', 'Void')
    Safety(make_sync(ALLOW_DELETE_VOID=True)).destructive('invoice', 'INV-1', 'Void')


def test_approved_document_not_overwritten():
    safety = Safety(make_sync())
    with pytest.raises(Hold, match='approved in Xero'):
        safety.approved_in_xero('invoice', 'AUTHORISED', {'LineItems': []})
    safety.approved_in_xero('invoice', 'DRAFT', {'LineItems': []})
    safety.approved_in_xero('invoice', 'AUTHORISED', {'Status': 'AUTHORISED'})  # status-only is fine


def test_checks_pass_for_a_good_document():
    assert Safety(make_sync()).problems('po', order_total(30.0), {'LineItems': LINES}) == []


def test_totals_must_match_inventree():
    problems = Safety(make_sync()).problems('po', order_total(42.0), {'LineItems': LINES})
    assert problems and 'add up to 30.00' in problems[0] and '42.00' in problems[0]


def test_unpriced_and_missing_tax_are_held():
    lines = [{'Description': 'Plug', 'Quantity': 2, 'UnitAmount': 0}]
    problems = Safety(make_sync(NO_TAX=True)).problems('so', None, {'LineItems': lines})
    assert any('tax type is not set' in p for p in problems)
    assert any('has no price' in p for p in problems)
    assert Safety(make_sync()).problems('quote', None, {'LineItems': lines}, placeholder=True) == []


def test_contact_matching_helpers():
    assert normalise_name('Acme Cables Pty. Ltd.') == normalise_name('acme cables')
    company = SimpleNamespace(email='orders@a.example', tax_id='12 345 678 901')
    assert contact_conflicts(company, {'EmailAddress': 'orders@a.example', 'TaxNumber': '12345678901'}) == []
    assert contact_conflicts(company, {'EmailAddress': 'x@b.example'})[0].startswith('email')
    assert contact_conflicts(company, {'TaxNumber': '98 765 432 109'})[0].startswith('tax number')


def test_idempotency_key_is_stable():
    assert idempotency_key('t', 'POST', 'Invoices', {'a': 1, 'b': 2}, 'd') == idempotency_key('t', 'POST', 'Invoices', {'b': 2, 'a': 1}, 'd')
    assert idempotency_key('t', 'POST', 'Invoices', {'a': 1}, 'd') != idempotency_key('t', 'POST', 'Invoices', {'a': 2}, 'd')
    assert idempotency_key('t', 'POST', 'Invoices', {'a': 1}, 'd1') != idempotency_key('t', 'POST', 'Invoices', {'a': 1}, 'd2')


def test_create_vs_update_detection():
    from inventree_xero.xero_client import is_create

    assert is_create({'Invoices': [{'Type': 'ACCREC', 'LineItems': []}]})
    assert not is_create({'Invoices': [{'InvoiceID': 'x', 'Status': 'VOIDED'}]})
    assert is_create({'Payments': [{'Invoice': {'InvoiceID': 'x'}, 'Amount': 5}]}), 'a payment is a new record'
    assert not is_create({'Quotes': [{'QuoteID': 'q', 'Status': 'SENT'}]})


def test_tokens_encrypted():
    stored = encrypt('secret-token')
    assert is_encrypted(stored) and 'secret-token' not in stored and decrypt(stored) == 'secret-token'
    assert decrypt('plain-legacy') == 'plain-legacy'


def test_least_privilege_scopes(monkeypatch):
    import sys
    import types

    helpers = types.ModuleType('InvenTree.helpers')
    helpers.str2bool = lambda v: str(v).lower() in ('1', 'true', 'yes')
    monkeypatch.setitem(sys.modules, 'InvenTree', types.ModuleType('InvenTree'))
    monkeypatch.setitem(sys.modules, 'InvenTree.helpers', helpers)
    base = {'SCOPES': '', 'CONTACT_SYNC': 'link', 'SHOP_PAYMENT_ACCOUNT': '090', 'AUTO_APPROVE': False}
    scopes = required_scopes(base.get)
    assert 'accounting.contacts.read' in scopes and 'accounting.payments' not in scopes
    assert 'accounting.payments' in required_scopes({**base, 'AUTO_APPROVE': True}.get)
