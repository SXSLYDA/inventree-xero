"""Unit tests for the pure helpers (no InvenTree needed): pytest tests/test_mapping.py"""

from inventree_xero.mapping import (
    deep_link,
    doc_info,
    lines_signature,
    meta_from_info,
    preserved_lines,
    tagged_description,
    xero_only_summary,
)
from inventree_xero.xero_client import parse_xero_date, xero_error_message

LINES = [
    {'LineItemID': 'a', 'ItemCode': '1-1234567', 'Description': 'Connector', 'Quantity': 10, 'UnitAmount': 1.5,
     'LineAmount': 15.0, 'TaxAmount': 1.5},
    {'LineItemID': 'b', 'Description': 'Crimping labour', 'Quantity': 2, 'UnitAmount': 85, 'LineAmount': 170.0,
     'AccountCode': '200', 'TaxType': 'OUTPUT', 'Tracking': [{'Name': 'Region', 'Option': 'WA'}]},
    {'LineItemID': 'c', 'Description': 'Freight', 'Quantity': 1, 'UnitAmount': 25, 'LineAmount': 25.0},
]


def test_preserved_lines_keeps_only_unowned_writable_fields():
    kept = preserved_lines(LINES, owned_ids=['a'])
    assert [k['LineItemID'] for k in kept] == ['b', 'c']
    assert 'LineAmount' not in kept[0] and 'TaxAmount' not in kept[0]
    assert kept[0]['Tracking'] == [{'Name': 'Region', 'Option': 'WA'}]


def test_preserved_lines_everything_owned():
    assert preserved_lines(LINES, owned_ids=['a', 'b', 'c']) == []


def test_xero_only_summary():
    summary = xero_only_summary(LINES, ['a'])
    assert summary == {'count': 2, 'total': 195.0, 'lines': ['Crimping labour', 'Freight']}


def test_meta_from_info_adds_summary_and_final():
    doc = {'InvoiceID': 'x', 'InvoiceNumber': 'INV-1', 'Status': 'PAID', 'LineItems': LINES,
           'UpdatedDateUTC': '/Date(1760000000000+0000)/', 'Total': 210, 'AmountDue': 0, 'AmountPaid': 210}
    meta = meta_from_info(doc_info('invoice', doc, '!demo'), {}, line_ids=['a'])
    assert meta['final'] is True
    assert meta['xero_only']['count'] == 2
    assert meta['url'].startswith('https://go.xero.com/organisationlogin/default.aspx?shortcode=!demo')


def test_bill_doc_info_and_link():
    info = doc_info('bill', {'InvoiceID': 'b1', 'InvoiceNumber': 'SUP-55', 'Status': 'DRAFT', 'Type': 'ACCPAY'})
    assert info['number'] == 'SUP-55' and 'AccountsPayable' in info['url']
    assert 'AccountsPayable' in deep_link('bill', 'b1', '')


def test_lines_signature_changes_with_quantity():
    changed = [dict(LINES[0], Quantity=11)]
    assert lines_signature(LINES[:1]) != lines_signature(changed)


def test_tagged_description_replaces_old_tag_and_fits():
    assert tagged_description('Cable order [Xero PO-1]', 'PO-2') == 'Cable order [Xero PO-2]'
    assert len(tagged_description('x' * 400, 'INV-0001')) <= 250


def test_dates_and_errors():
    assert parse_xero_date('/Date(1518685950940+0000)/').year == 2018
    assert parse_xero_date('2026-10-20T00:00:00').tzinfo is not None
    assert parse_xero_date('not a date') is None
    body = {'Elements': [{'ValidationErrors': [{'Message': 'Item code X is not valid'}]}]}
    assert xero_error_message(body) == 'Item code X is not valid'
