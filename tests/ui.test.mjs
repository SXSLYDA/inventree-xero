// Render tests for static/xero_ui.js in jsdom, using InvenTree's legacy (target, context) entry convention.
// Run: npm install jsdom && node --test tests/ui.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { JSDOM } = require(process.env.JSDOM_PATH || 'jsdom');

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://inventree.example.com/' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const here = path.dirname(fileURLToPath(import.meta.url));
const ui = await import(pathToFileURL(path.join(here, '..', 'inventree_xero', 'static', 'xero_ui.js')).href);

const FIXTURES = {
  'order/po/7/': {
    reference: 'PO-0007', total: '102.00 AUD', connected: true, bills_mode: 'on_receipt',
    xero: { linked: true, number: 'PO-0007', status: 'AUTHORISED', url: 'https://go.xero.com/x', total: 127,
            synced: '2026-10-20T01:00:00Z', xero_only: { count: 1, total: 25, lines: ['Freight (added in Xero)'] } },
    bill: { linked: true, number: 'SUP-INV-55', status: 'DRAFT', total: 60, amount_due: 60, amount_paid: 0 },
    log: [{ created: '2026-10-20T01:00:00Z', level: 'info', direction: 'export', doc_type: 'po', message: 'PO-0007 → Xero' }],
  },
  'order/so/9/': {
    reference: 'SO-0009', total: '480.00 AUD', connected: true,
    xero: { linked: true, number: 'INV-0042', status: 'PAID', doc_type: 'invoice', amount_due: 0, amount_paid: 528 },
    shop: { number: '1001', on_account: false, payment_recorded: true }, log: [],
  },
  'company/3/': {
    name: 'Mine Site', connected: true,
    xero: { linked: true, contact_name: 'Mine Site Pty Ltd', contact_id: 'abc',
            balance: { receivable_outstanding: 1540.5, receivable_overdue: 220, payable_outstanding: 0, payable_overdue: 0 } },
  },
  'part/4/': { name: '1-1234567', code: '1-1234567', connected: true, xero: { linked: false } },
  'status/': { connected: true, tenant_name: 'Demo Company (AU)', auth_mode: 'oauth', redirect_uri: 'https://inventree.example.com/plugin/xero/oauth/callback/',
               connect_url: '/plugin/xero/oauth/connect/', scheduled: true, interval_minutes: 15, po_direction: 'both',
               so_direction: 'both', so_document: 'INVOICE', recent: [], errors_24h: 2 },
};

function ctxFor(context) {
  const calls = [];
  return {
    calls,
    context: { api: '/plugin/xero/api/', ...context },
    api: {
      request: async ({ method, url, data }) => {
        calls.push({ method, url, data });
        const key = url.replace('/plugin/xero/api/', '');
        if (key === 'order/po/7/preview/') {
          return { data: { kind: 'po', write_mode: 'dry_run', action: 'create purchase order: 1 line(s), 20.00 Exclusive (DRAFT)',
            problems: ['the purchase tax type is not set in the plugin settings (e.g. INPUT for Australian GST)'],
            payload: { status: 'DRAFT', total: 20, contact_linked: true,
              lines: [{ description: 'SAFE-1 - Connector', item_code: '1-1234567', quantity: 10, unit_amount: 2, account: '300', tax_type: null }] } } };
        }
        if (key.startsWith('company/3/candidates/')) {
          return { data: { query: 'Mine Site', results: [{ contact_id: 'c-9', name: 'Mine Site Pty Ltd', email: 'ap@mine.example',
            tax_number: '', conflicts: [] }] } };
        }
        if (key === 'check/' && method === 'GET') {
          return { data: { last: { created: '2026-10-20T01:00:00Z', message: 'inventree-xero configuration check\n12 passed, 1 warnings, 0 failed\n\n[PASS] Purchase account: 300 Purchases (DIRECTCOSTS)\n[WARN] Email: InvenTree email is not configured' } } };
        }
        if (key === 'selftest/' && method === 'GET') {
          return { data: { last: null } };
        }
        if (key.startsWith('audit/')) {
          return { data: [{ created: '2026-10-20T01:00:00Z', mode: 'live', method: 'POST', path: 'PurchaseOrders',
            inventree_ref: 'PO-0007', response_status: 200, ok: true, error: '' }] };
        }
        if (key.startsWith('quotes/')) {
          return { data: { available: true, results: [{ id: 5, reference: 'QR-261020-AB12', customer: 'Mine Site', status: 'Quoted',
            valid_until: '2026-11-03', xero: { number: 'QU-0007', status: 'SENT', url: 'https://go.xero.com/q', total: '2942.10',
            error: '' } }] } };
        }
        if (key.startsWith('search/')) {
          return { data: { count: 1, results: [{ kind: 'so', pk: 9, reference: 'SO-0009', party: 'Mine Site', status: 'In Progress',
            url: '/web/sales/sales-order/9', total: '480', xero: { linked: true, status: 'PAID', number: 'INV-0042' },
            shop: { number: '1001' } }] } };
        }
        if (method === 'POST') return { data: { message: 'Done', ok: true } };
        if (!(key in FIXTURES)) throw Object.assign(new Error('404'), { response: { data: { error: 'not found' } } });
        return { data: FIXTURES[key] };
      },
    },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 20));

function mount(fn, context) {
  const target = document.createElement('div');
  document.body.appendChild(target);
  const ctx = ctxFor(context);
  assert.equal(fn.length, 2, 'entry points must take (target, context) so InvenTree uses the legacy renderer');
  fn(target, ctx);
  return { target, ctx };
}

test('purchase order panel shows Xero PO, Xero-only lines and bill', async () => {
  const { target } = mount(ui.renderXeroPanel, { kind: 'po', pk: 7 });
  await settle();
  const text = target.textContent;
  assert.match(text, /Purchase order PO-0007/);
  assert.match(text, /Kept in Xero only/);
  assert.match(text, /Freight \(added in Xero\)/);
  assert.match(text, /SUP-INV-55/);
  assert.match(text, /Update bill now/);
});

test('sales order panel shows web order and recorded payment', async () => {
  const { target } = mount(ui.renderXeroPanel, { kind: 'so', pk: 9 });
  await settle();
  assert.match(target.textContent, /Sales invoice INV-0042/);
  assert.match(target.textContent, /web order 1001 · paid online/);
  assert.match(target.textContent, /payment recorded/);
});

test('company panel shows balances', async () => {
  const { target } = mount(ui.renderXeroPanel, { kind: 'company', pk: 3 });
  await settle();
  assert.match(target.textContent, /Owes us/);
  assert.match(target.textContent, /220 overdue/);
});

test('part panel offers to link or create the item, and posts the action', async () => {
  const { target, ctx } = mount(ui.renderXeroPanel, { kind: 'part', pk: 4 });
  await settle();
  assert.match(target.textContent, /Item code would be 1-1234567/);
  target.querySelector('button[data-act="create"]').click();
  await settle();
  assert.deepEqual(ctx.calls.find((c) => c.method === 'POST'), { method: 'POST', url: '/plugin/xero/api/part/4/', data: { action: 'create' } });
});

test('dashboard searches and shows results', async () => {
  const { target, ctx } = mount(ui.renderXeroDashboard, {});
  await settle();
  assert.match(target.textContent, /Xero: Demo Company \(AU\)/);
  assert.match(target.textContent, /SO-0009/);
  assert.match(target.textContent, /web 1001/);
  assert.ok(ctx.calls.some((c) => c.url.startsWith('/plugin/xero/api/search/?')));
});

test('admin panel shows redirect URI and sync buttons', async () => {
  const { target } = mount(ui.renderXeroAdmin, {});
  await settle();
  assert.match(target.textContent, /plugin\/xero\/oauth\/callback\//);
  assert.ok(target.querySelector('button[data-act="sync-all"]'));
});

test('errors from the API are shown, not thrown', async () => {
  const { target } = mount(ui.renderXeroPanel, { kind: 'po', pk: 999 });
  await settle();
  assert.match(target.textContent, /not found/);
});

test('dashboard lists web shop quotes with their Xero quote', async () => {
  const { target, ctx } = mount(ui.renderXeroDashboard, {});
  await settle();
  const form = target.querySelector('form');
  form.elements.type.value = 'quotes';
  form.dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  assert.match(target.textContent, /QR-261020-AB12/);
  assert.match(target.textContent, /QU-0007/);
  assert.ok(ctx.calls.some((c) => c.url.startsWith('/plugin/xero/api/quotes/')));
  target.querySelector('button[data-act="pull"]').click();
  await settle();
  assert.ok(ctx.calls.some((c) => c.method === 'POST' && c.url === '/plugin/xero/api/quotes/5/pull/'));
});

test('sales order panel shows the quote it came from', async () => {
  FIXTURES['order/so/9/'].quote = { reference: 'QR-261020-AB12', number: 'QU-0007', status: 'INVOICED', url: 'https://go.xero.com/q' };
  const { target } = mount(ui.renderXeroPanel, { kind: 'so', pk: 9 });
  await settle();
  assert.match(target.textContent, /From web quote QR-261020-AB12/);
});

test('order panel shows dry-run banner, a hold reason and the preview', async () => {
  const fx = FIXTURES['order/po/7/'];
  Object.assign(fx, { write_mode: 'dry_run' });
  fx.xero.held = 'Not sent to Xero: "SAFE-1" has no price.';
  const { target } = mount(ui.renderXeroPanel, { kind: 'po', pk: 7 });
  await settle();
  assert.match(target.textContent, /Dry run/);
  assert.match(target.textContent, /Held — not sent to Xero: Not sent to Xero: "SAFE-1" has no price/);
  target.querySelector('button[data-act="preview"]').click();
  await settle();
  assert.match(target.textContent, /Would be held:/);
  assert.match(target.textContent, /tax type is not set/);
  assert.match(target.textContent, /SAFE-1 - Connector/);
  delete fx.xero.held;
});

test('company panel finds a Xero contact and links it by hand', async () => {
  FIXTURES['company/3/'] = { name: 'Mine Site', connected: true, xero: { linked: false } };
  const { target, ctx } = mount(ui.renderXeroPanel, { kind: 'company', pk: 3 });
  await settle();
  const form = target.querySelector('form[data-find]');
  form.dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  assert.match(target.textContent, /Mine Site Pty Ltd/);
  target.querySelector('button[data-link="c-9"]').click();
  await settle();
  assert.ok(ctx.calls.some((c) => c.method === 'POST' && c.url === '/plugin/xero/api/company/3/'
    && c.data.action === 'link_id' && c.data.contact_id === 'c-9'));
});

test('settings panel shows the safety card, recent writes and the kill switch', async () => {
  Object.assign(FIXTURES['status/'], { write_mode: 'live', auto_approve: false, allow_delete_void: false, held: 3,
    required_scopes: ['offline_access', 'accounting.invoices'], missing_scopes: ['accounting.contacts'] });
  const { target, ctx } = mount(ui.renderXeroAdmin, {});
  await settle();
  assert.match(target.textContent, /LIVE — writing to Xero/);
  assert.match(target.textContent, /Drafts only — you approve in Xero/);
  assert.match(target.textContent, /Reconnect to grant: accounting.contacts/);
  assert.match(target.textContent, /POST PurchaseOrders/);
  target.querySelector('button[data-mode="paused"]').click();
  await settle();
  assert.ok(ctx.calls.some((c) => c.method === 'POST' && c.url === '/plugin/xero/api/mode/' && c.data.mode === 'paused'));
});

test('settings panel shows the last configuration check and the self-test button', async () => {
  const { target } = mount(ui.renderXeroAdmin, {});
  await settle();
  assert.match(target.textContent, /Last configuration check/);
  assert.match(target.textContent, /300 Purchases/);
  assert.ok(target.querySelector('button[data-act="check"]'));
  assert.ok(target.querySelector('button[data-act="selftest"]'));
});
