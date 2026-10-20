/*
 * Xero plugin UI for InvenTree — order/company panels, dashboard search widget,
 * and the plugin settings (admin) panel. Plain JS, no build step.
 *
 * Works with both InvenTree plugin calling conventions:
 *   fn(targetElement, context)   → renders into the element
 *   fn(context)                  → returns a React element (window.React)
 */

const DEFAULT_API = '/plugin/xero/api/';

/* ------------------------------------------------------------------ helpers */

function getCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

function apiBase(ctx) {
  return (ctx && ctx.context && ctx.context.api) || DEFAULT_API;
}

async function call(ctx, method, path, body) {
  const url = apiBase(ctx) + path;
  if (ctx && ctx.api && typeof ctx.api.request === 'function') {
    try {
      const r = await ctx.api.request({ method, url, data: body });
      return r.data;
    } catch (err) {
      const d = (err.response && err.response.data) || {};
      throw new Error(d.error || d.detail || d.message || err.message);
    }
  }
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-CSRFToken'] = getCookie('csrftoken');
  }
  const r = await fetch(url, {
    method, credentials: 'same-origin', headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || data.message || r.statusText);
  return data;
}

function esc(v) {
  return String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString();
}

function navigate(ctx, url) {
  if (ctx && typeof ctx.navigate === 'function') ctx.navigate(url);
  else window.location.href = url;
}

const STYLE = `
.xero-ui{font-size:14px;line-height:1.45}
.xero-ui .xr-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
.xero-ui .xr-grid{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:8px 0}
.xero-ui .xr-grid dt{opacity:.7}
.xero-ui .xr-grid dd{margin:0;word-break:break-word}
.xero-ui button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid rgba(128,128,128,.45);
  background:transparent;color:inherit;cursor:pointer}
.xero-ui button.primary{background:#13b5ea;border-color:#13b5ea;color:#fff}
.xero-ui button.danger{border-color:#e03131;color:#e03131}
.xero-ui button:disabled{opacity:.5;cursor:wait}
.xero-ui input,.xero-ui select{font:inherit;padding:5px 8px;border-radius:6px;
  border:1px solid rgba(128,128,128,.45);background:transparent;color:inherit}
.xero-ui input{flex:1;min-width:160px}
.xero-ui .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;
  background:rgba(128,128,128,.18)}
.xero-ui .badge.ok{background:rgba(47,158,68,.18);color:#2f9e44}
.xero-ui .badge.warn{background:rgba(240,140,0,.18);color:#e8590c}
.xero-ui .badge.err{background:rgba(224,49,49,.18);color:#e03131}
.xero-ui .badge.info{background:rgba(19,181,234,.18);color:#0c8bb5}
.xero-ui .msg{margin:6px 0;padding:6px 10px;border-radius:6px;background:rgba(128,128,128,.12)}
.xero-ui .msg.err{background:rgba(224,49,49,.12);color:#e03131}
.xero-ui table{width:100%;border-collapse:collapse;font-size:13px}
.xero-ui th,.xero-ui td{text-align:left;padding:5px 6px;border-bottom:1px solid rgba(128,128,128,.2);vertical-align:top}
.xero-ui th{font-weight:600;opacity:.75}
.xero-ui .tbl{overflow-x:auto}
.xero-ui a{color:#0c8bb5}
.xero-ui code{font-size:12px;padding:1px 4px;border-radius:4px;background:rgba(128,128,128,.15);word-break:break-all}
.xero-ui h4{margin:12px 0 4px;font-size:14px}
.xero-ui .muted{opacity:.65}
`;

function shell(target) {
  target.innerHTML = '';
  const style = document.createElement('style');
  style.textContent = STYLE;
  const root = document.createElement('div');
  root.className = 'xero-ui';
  target.appendChild(style);
  target.appendChild(root);
  return root;
}

function statusBadge(status) {
  if (!status) return '<span class="badge">not linked</span>';
  const cls = {
    PAID: 'ok', AUTHORISED: 'ok', BILLED: 'ok', ACCEPTED: 'ok', INVOICED: 'ok',
    DRAFT: 'info', SUBMITTED: 'info', SENT: 'info',
    VOIDED: 'err', DELETED: 'err', DECLINED: 'err',
  }[status] || '';
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function logTable(rows) {
  if (!rows || !rows.length) return '<p class="muted">No activity yet.</p>';
  return `<div class="tbl"><table><thead><tr><th>When</th><th></th><th>Type</th><th>Message</th></tr></thead><tbody>${
    rows.map((r) => `<tr><td>${fmtDate(r.created)}</td>
      <td><span class="badge ${r.level === 'error' ? 'err' : r.level === 'warning' ? 'warn' : ''}">${esc(r.level)}</span></td>
      <td>${esc(r.direction)} ${esc(r.doc_type)}</td>
      <td style="white-space:pre-wrap">${esc(r.message)}</td></tr>`).join('')
  }</tbody></table></div>`;
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = label; }
}

/* ------------------------------------------------------------------ order / company panel */

function xeroOnlyBlock(x) {
  const xo = x && x.xero_only;
  if (!xo || !xo.count) return '';
  return `<div class="msg">Kept in Xero only (not parts): ${xo.count} line(s), ${esc(xo.total)}
    <div class="muted">${xo.lines.map(esc).join(' · ')}</div></div>`;
}

function modeBanner(mode) {
  if (mode === 'paused') return '<div class="msg err"><b>Writes to Xero are paused</b> (kill switch) — nothing is sent.</div>';
  if (mode === 'dry_run' || !mode) return '<div class="msg"><b>Dry run</b> — the plugin shows what it would send to Xero but writes nothing.</div>';
  return '';
}

function previewBlock(p) {
  if (!p) return '';
  if (p.note && !p.payload) return `<div class="msg">Preview: nothing to send — ${esc(p.note)}</div>`;
  const rows = ((p.payload || {}).lines || []).map((l) => `<tr><td>${esc(l.description)}</td><td>${esc(l.item_code || '')}</td>
    <td>${esc(l.quantity)}</td><td>${esc(l.unit_amount)}</td><td>${esc(l.discount || '')}</td><td>${esc(l.account || '—')}</td><td>${esc(l.tax_type || '—')}</td></tr>`).join('');
  return `<div class="msg ${p.problems && p.problems.length ? 'err' : ''}">
    <b>Preview:</b> ${esc(p.action || '')}${p.unchanged ? ' — unchanged since last sync' : ''}
    ${p.problems && p.problems.length ? `<div><b>Would be held:</b><ul>${p.problems.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>` : '<div>Passes all checks.</div>'}
    ${p.payload && !p.payload.contact_linked ? '<div>The contact isn\'t linked yet — it would be matched (or held if uncertain).</div>' : ''}
  </div>
  ${rows ? `<div class="tbl"><table><thead><tr><th>Line</th><th>Item</th><th>Qty</th><th>Unit</th><th>Disc %</th><th>Account</th><th>Tax</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}`;
}

function reportBlock(title, report) {
  if (!report) return '';
  const text = report.text || report.message || '';
  const levels = { PASS: 'ok', WARN: 'warn', FAIL: 'err', INFO: 'info' };
  const rows = text.split('\n').map((line) => {
    const m = line.match(/^\[(PASS|WARN|FAIL|INFO)\] ([^:]+): (.*)$/);
    return m ? `<tr><td><span class="badge ${levels[m[1]]}">${m[1]}</span></td><td>${esc(m[2])}</td><td style="white-space:pre-wrap">${esc(m[3])}</td></tr>` : '';
  }).join('');
  const head = text.split('\n').slice(0, 2).map(esc).join(' — ');
  return `<h4>${esc(title)}</h4><p class="muted">${head}${report.created ? ' · ' + fmtDate(report.created) : ''}</p>
    ${rows ? `<div class="tbl"><table><tbody>${rows}</tbody></table></div>` : ''}
    <details><summary class="muted">Copy as text</summary><textarea readonly rows="8" style="width:100%;font:12px monospace">${esc(text)}</textarea></details>`;
}

function money(v) {
  return v === null || v === undefined || v === '' ? '—' : esc(v);
}

async function renderOrder(root, ctx, kind, pk) {
  let message = null;
  let preview = null;
  const draw = async () => {
    let d;
    try {
      d = await call(ctx, 'GET', `order/${kind}/${pk}/`);
    } catch (e) {
      root.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
      return;
    }
    const x = d.xero || {};
    const b = d.bill || {};
    const docLabel = kind === 'po' ? 'Purchase order' : (x.doc_type === 'quote' ? 'Quote' : 'Sales invoice');
    const billBlock = kind !== 'po' ? '' : `
      <h4>Bill</h4>
      <div class="xr-row">${b.linked ? statusBadge(b.status) : '<span class="badge">no bill</span>'}
        ${b.linked ? `<b>${esc(b.number || 'Bill')}</b>` : `<span class="muted">${d.bills_mode === 'off'
          ? 'Bills are turned off in the plugin settings' : 'Created once stock is received'}</span>`}
        ${b.origin === 'xero' ? '<span class="badge info">entered in Xero</span>' : ''}</div>
      ${b.error ? `<div class="msg err">Bill error: ${esc(b.error)}</div>` : ''}
      ${b.held ? `<div class="msg err"><b>Bill held:</b> ${esc(b.held)}</div>` : ''}
      ${b.dry_run ? `<div class="msg">Dry run — would ${esc(b.dry_run)}</div>` : ''}
      ${b.linked ? `<dl class="xr-grid"><dt>Bill total</dt><dd>${money(b.total)}</dd>
        <dt>Amount due</dt><dd>${money(b.amount_due)}</dd><dt>Amount paid</dt><dd>${money(b.amount_paid)}</dd></dl>` : ''}
      ${xeroOnlyBlock(b)}
      <div class="xr-row">
        <button data-act="bill">${b.linked ? 'Update bill now' : 'Create bill now'}</button>
        <button data-act="preview-bill">Preview bill</button>
        ${b.url ? `<a href="${esc(b.url)}" target="_blank" rel="noopener">Open bill in Xero ↗</a>` : ''}
        ${b.linked ? '<button class="danger" data-act="unlink-bill">Unlink bill</button>' : ''}
      </div>`;
    root.innerHTML = `
      ${modeBanner(d.write_mode)}
      ${message ? `<div class="msg ${message.ok ? '' : 'err'}">${esc(message.text)}</div>` : ''}
      ${!d.connected ? '<div class="msg err">Xero is not connected — connect it from the plugin settings.</div>' : ''}
      <div class="xr-row">${statusBadge(x.status)}
        ${x.linked ? `<b>${esc(docLabel)} ${esc(x.number || '')}</b>` : '<span class="muted">This order is not in Xero yet</span>'}
        ${x.origin === 'xero' ? '<span class="badge info">imported from Xero</span>' : ''}
        ${d.shop ? `<span class="badge info">web order ${esc(d.shop.number || '')}${d.shop.on_account ? ' · on account' : ' · paid online'}</span>` : ''}
        ${d.shop && d.shop.payment_recorded ? '<span class="badge ok">payment recorded</span>' : ''}
      </div>
      ${d.quote ? `<div class="xr-row muted">From web quote ${esc(d.quote.reference)} — Xero ${d.quote.url
        ? `<a href="${esc(d.quote.url)}" target="_blank" rel="noopener">${esc(d.quote.number)}</a>` : esc(d.quote.number)}
        ${statusBadge(d.quote.status)}</div>` : ''}
      ${x.error ? `<div class="msg err">Last sync error: ${esc(x.error)}</div>` : ''}
      ${x.held ? `<div class="msg err"><b>Held — not sent to Xero:</b> ${esc(x.held)}</div>` : ''}
      ${x.dry_run ? `<div class="msg">Dry run — would ${esc(x.dry_run)}</div>` : ''}
      ${previewBlock(preview)}
      <dl class="xr-grid">
        <dt>InvenTree total</dt><dd>${money(d.total)}</dd>
        ${x.linked ? `<dt>Xero total</dt><dd>${money(x.total)}</dd>` : ''}
        ${x.linked && kind === 'so' ? `<dt>Amount due</dt><dd>${money(x.amount_due)}</dd><dt>Amount paid</dt><dd>${money(x.amount_paid)}</dd>` : ''}
        <dt>Last synced</dt><dd>${fmtDate(x.synced)}</dd>
      </dl>
      ${xeroOnlyBlock(x)}
      <div class="xr-row">
        <button data-act="preview">Preview</button>
        <button class="primary" data-act="push">${x.linked ? 'Update Xero now' : 'Send to Xero'}</button>
        ${x.linked || b.linked ? '<button data-act="pull">Refresh from Xero</button>' : ''}
        ${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">Open in Xero ↗</a>` : ''}
        ${x.linked ? '<button class="danger" data-act="unlink" title="Remove the link only; Xero is unchanged">Unlink</button>' : ''}
      </div>
      ${billBlock}
      <h4>Recent Xero activity</h4>${logTable(d.log)}`;
    root.querySelectorAll('button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        const act = btn.dataset.act;
        if (act.startsWith('unlink') && !window.confirm('Remove this Xero link? The Xero document is not changed.')) return;
        try {
          if (act.startsWith('preview')) {
            preview = await call(ctx, 'GET', `order/${kind}/${pk}/${act}/`);
            message = null;
            await draw();
            return;
          }
          const r = await call(ctx, 'POST', `order/${kind}/${pk}/${act}/`, {});
          preview = null;
          message = { ok: r.ok !== false, text: r.message || 'Done' };
        } catch (e) {
          message = { ok: false, text: e.message };
        }
        await draw();
      }));
    });
  };
  await draw();
}

async function renderCompany(root, ctx, pk) {
  let message = null;
  let found = null;
  const draw = async () => {
    let d;
    try {
      d = await call(ctx, 'GET', `company/${pk}/`);
    } catch (e) {
      root.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
      return;
    }
    const x = d.xero || {};
    const bal = x.balance;
    root.innerHTML = `
      ${message ? `<div class="msg ${message.ok ? '' : 'err'}">${esc(message.text)}</div>` : ''}
      <div class="xr-row">${x.linked ? '<span class="badge ok">linked</span>' : '<span class="badge">not linked</span>'}
        ${x.linked ? `Xero contact <b>${esc(x.contact_name)}</b>` : '<span class="muted">No Xero contact linked</span>'}</div>
      <dl class="xr-grid"><dt>Contact ID</dt><dd>${x.contact_id ? `<code>${esc(x.contact_id)}</code>` : '—'}</dd>
        <dt>Linked</dt><dd>${fmtDate(x.synced)}</dd>
        ${bal ? `<dt>Owes us</dt><dd>${money(bal.receivable_outstanding)}
            ${Number(bal.receivable_overdue) > 0 ? `<span class="badge err">${esc(bal.receivable_overdue)} overdue</span>` : ''}</dd>
          <dt>We owe</dt><dd>${money(bal.payable_outstanding)}
            ${Number(bal.payable_overdue) > 0 ? `<span class="badge warn">${esc(bal.payable_overdue)} overdue</span>` : ''}</dd>
          <dt>Balances as at</dt><dd>${fmtDate(bal.updated)}</dd>` : ''}
      </dl>
      <div class="xr-row">
        ${!x.linked ? '<button class="primary" data-act="link">Find / link Xero contact</button><button data-act="create">Link or create contact</button>' : ''}
        ${x.linked ? '<button data-act="balance">Refresh balance</button><button data-act="push" title="Send this company\'s details to the Xero contact">Push details to Xero</button>' : ''}
        ${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">Open in Xero ↗</a>` : ''}
        ${x.linked ? '<button class="danger" data-act="unlink">Unlink</button>' : ''}
      </div>
      ${!x.linked ? `<form class="xr-row" data-find><input name="q" placeholder="Find the contact in Xero by name…" /><button type="submit">Find in Xero</button></form>` : ''}
      ${found ? (found.results.length ? `<div class="tbl"><table><thead><tr><th>Xero contact</th><th>Email</th><th>ABN / tax no.</th><th></th></tr></thead><tbody>${
        found.results.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.email || '')}</td><td>${esc(c.tax_number || '')}</td>
          <td>${c.conflicts.length ? `<span class="badge warn" title="${esc(c.conflicts.join('; '))}">differs</span> ` : ''}
          <button data-link="${esc(c.contact_id)}">Link</button></td></tr>`).join('')}</tbody></table></div>`
        : `<p class="muted">No Xero contacts match "${esc(found.query)}".</p>`) : ''}`;
    const findForm = root.querySelector('[data-find]');
    if (findForm) {
      findForm.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        try {
          const q = findForm.elements.q.value.trim();
          found = await call(ctx, 'GET', `company/${pk}/candidates/${q ? `?q=${encodeURIComponent(q)}` : ''}`);
        } catch (e) { message = { ok: false, text: e.message }; }
        await draw();
      });
    }
    root.querySelectorAll('button[data-link]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        try {
          await call(ctx, 'POST', `company/${pk}/`, { action: 'link_id', contact_id: btn.dataset.link });
          message = { ok: true, text: 'Linked' };
          found = null;
        } catch (e) { message = { ok: false, text: e.message }; }
        await draw();
      }));
    });
    root.querySelectorAll('button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        try {
          await call(ctx, 'POST', `company/${pk}/`, { action: btn.dataset.act });
          message = { ok: true, text: 'Done' };
        } catch (e) {
          message = { ok: false, text: e.message };
        }
        await draw();
      }));
    });
  };
  await draw();
}

async function renderPart(root, ctx, pk) {
  let message = null;
  const draw = async () => {
    let d;
    try {
      d = await call(ctx, 'GET', `part/${pk}/`);
    } catch (e) {
      root.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
      return;
    }
    const x = d.xero || {};
    root.innerHTML = `
      ${message ? `<div class="msg ${message.ok ? '' : 'err'}">${esc(message.text)}</div>` : ''}
      <div class="xr-row">${x.linked ? '<span class="badge ok">linked</span>' : '<span class="badge">not linked</span>'}
        ${x.linked ? `Xero item <b>${esc(x.item_code)}</b>` : `<span class="muted">Item code would be <code>${esc(d.code || '—')}</code></span>`}</div>
      <div class="xr-row">
        ${!x.linked && d.code ? '<button class="primary" data-act="link">Link Xero item</button><button data-act="create">Create Xero item</button>' : ''}
        ${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">Open in Xero ↗</a>` : ''}
        ${x.linked ? '<button class="danger" data-act="unlink">Unlink</button>' : ''}
      </div>`;
    root.querySelectorAll('button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        try {
          await call(ctx, 'POST', `part/${pk}/`, { action: btn.dataset.act });
          message = { ok: true, text: 'Done' };
        } catch (e) {
          message = { ok: false, text: e.message };
        }
        await draw();
      }));
    });
  };
  await draw();
}

function panel(target, ctx) {
  const root = shell(target);
  const c = ctx.context || {};
  const pk = c.pk || ctx.id;
  if (c.kind === 'company') renderCompany(root, ctx, pk);
  else if (c.kind === 'part') renderPart(root, ctx, pk);
  else renderOrder(root, ctx, c.kind, pk);
}

/* ------------------------------------------------------------------ dashboard: search + status */

function resultsTable(ctx, rows) {
  if (!rows.length) return '<p class="muted">No matching orders.</p>';
  const wrap = document.createElement('div');
  wrap.className = 'tbl';
  wrap.innerHTML = `<table><thead><tr><th>Order</th><th>Party</th><th>Status</th><th>Xero</th><th>Bill</th><th>Total</th></tr></thead><tbody>${
    rows.map((r, i) => `<tr>
      <td><a href="${esc(r.url)}" data-i="${i}">${esc(r.reference)}</a> <span class="muted">${r.kind === 'po' ? 'PO' : 'SO'}</span>
        ${r.party_reference ? `<div class="muted">${esc(r.party_reference)}</div>` : ''}</td>
      <td>${esc(r.party)}</td>
      <td>${esc(r.status)}</td>
      <td>${r.xero.linked ? `${statusBadge(r.xero.status)} ${r.xero.url
        ? `<a href="${esc(r.xero.url)}" target="_blank" rel="noopener">${esc(r.xero.number || 'open')}</a>`
        : esc(r.xero.number || '')}` : '<span class="badge">not linked</span>'}
        ${r.xero.error ? '<span class="badge err" title="' + esc(r.xero.error) + '">error</span>' : ''}
        ${r.shop ? `<span class="badge info">web ${esc(r.shop.number || '')}</span>` : ''}</td>
      <td>${r.bill && r.bill.linked ? `${statusBadge(r.bill.status)} ${r.bill.url
        ? `<a href="${esc(r.bill.url)}" target="_blank" rel="noopener">${esc(r.bill.number || 'bill')}</a>` : ''}` : ''}</td>
      <td>${esc(r.total ?? '')}</td></tr>`).join('')
  }</tbody></table>`;
  wrap.querySelectorAll('a[data-i]').forEach((a) => {
    a.addEventListener('click', (ev) => { ev.preventDefault(); navigate(ctx, a.getAttribute('href')); });
  });
  return wrap;
}

function quotesTable(rows) {
  if (!rows.length) return '<p class="muted">No quote requests.</p>';
  return `<div class="tbl"><table><thead><tr><th>Quote</th><th>Customer</th><th>Shop</th><th>Xero quote</th><th>Valid until</th><th></th></tr></thead><tbody>${
    rows.map((r) => {
      const x = r.xero || {};
      return `<tr><td>${esc(r.reference)}</td><td>${esc(r.customer)}</td><td>${esc(r.status)}</td>
        <td>${x.number ? `${statusBadge(x.status)} ${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.number)}</a>` : esc(x.number)}
          ${x.total ? `<span class="muted">${esc(x.total)}</span>` : ''}` : '<span class="badge">not in Xero</span>'}
          ${x.error ? `<div class="msg err">${esc(x.error)}</div>` : ''}</td>
        <td>${r.valid_until ? esc(r.valid_until) : '—'}</td>
        <td><button data-quote="${r.id}" data-act="push">Send to Xero</button>
          ${x.number ? `<button data-quote="${r.id}" data-act="pull">Refresh from Xero</button>` : ''}</td></tr>`;
    }).join('')
  }</tbody></table></div>`;
}

function dashboard(target, ctx) {
  const root = shell(target);
  root.innerHTML = `
    <div class="xr-row" data-status><span class="muted">Loading Xero status…</span></div>
    <form class="xr-row" data-form>
      <input name="q" placeholder="Search order no., supplier/customer, Xero INV/PO number…" />
      <select name="type"><option value="all">All orders</option><option value="po">Purchase orders</option><option value="so">Sales orders</option><option value="quotes">Web shop quotes</option></select>
      <select name="linked"><option value="any">Any</option><option value="yes">In Xero</option><option value="no">Not in Xero</option><option value="held">Held for review</option><option value="error">Sync errors</option></select>
      <select name="source"><option value="">All sources</option><option value="shop">Web shop orders</option></select>
      <select name="xero_status"><option value="">Any Xero status</option>
        ${['DRAFT', 'SUBMITTED', 'AUTHORISED', 'BILLED', 'PAID', 'SENT', 'ACCEPTED', 'VOIDED', 'DELETED']
          .map((s) => `<option>${s}</option>`).join('')}</select>
      <button class="primary" type="submit">Search</button>
    </form>
    <div data-results></div>`;
  const statusEl = root.querySelector('[data-status]');
  const resultsEl = root.querySelector('[data-results]');
  const form = root.querySelector('[data-form]');

  const loadStatus = async () => {
    try {
      const s = await call(ctx, 'GET', 'status/');
      statusEl.innerHTML = `
        ${s.connected ? `<span class="badge ok">Xero: ${esc(s.tenant_name)}</span>` : '<span class="badge err">Xero not connected</span>'}
        <span class="muted">Last sync ${fmtDate(s.last_sync_finished)}</span>
        ${s.last_sync_ok === false ? '<span class="badge err">last sync had errors</span>' : ''}
        ${s.errors_24h ? `<span class="badge warn">${s.errors_24h} errors / 24h</span>` : ''}
        ${s.held ? `<span class="badge err">${s.held} held for review</span>` : ''}
        ${s.write_mode === 'live' ? '<span class="badge ok">live</span>' : s.write_mode === 'paused'
          ? '<span class="badge err">PAUSED</span>' : '<span class="badge info">dry run</span>'}
        <button data-sync>Sync now</button>`;
      const btn = statusEl.querySelector('[data-sync]');
      btn.addEventListener('click', () => busy(btn, async () => {
        try { await call(ctx, 'POST', 'sync/', { direction: 'all' }); btn.textContent = 'Queued'; }
        catch (e) { window.alert(e.message); }
      }));
    } catch (e) {
      statusEl.innerHTML = `<span class="msg err">${esc(e.message)}</span>`;
    }
  };

  const quoteSearch = async () => {
    const p = new URLSearchParams();
    const q = form.elements.q.value;
    if (q) p.set('q', q);
    if (form.elements.linked.value === 'error') p.set('status', 'error');
    resultsEl.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const d = await call(ctx, 'GET', `quotes/?${p.toString()}`);
      if (!d.available) { resultsEl.innerHTML = '<p class="muted">The web shop plugin is not installed.</p>'; return; }
      resultsEl.innerHTML = quotesTable(d.results || []);
      resultsEl.querySelectorAll('button[data-quote]').forEach((btn) => {
        btn.addEventListener('click', () => busy(btn, async () => {
          try {
            const r = await call(ctx, 'POST', `quotes/${btn.dataset.quote}/${btn.dataset.act}/`, {});
            btn.textContent = r.message ? 'Done' : 'OK';
          } catch (e) { window.alert(e.message); }
          await quoteSearch();
        }));
      });
    } catch (e) {
      resultsEl.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    }
  };

  const search = async () => {
    if (form.elements.type.value === 'quotes') { await quoteSearch(); return; }
    const p = new URLSearchParams();
    Array.from(form.elements).forEach((el) => { if (el.name && el.value) p.set(el.name, el.value); });
    p.set('limit', '50');
    resultsEl.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const d = await call(ctx, 'GET', `search/?${p.toString()}`);
      resultsEl.innerHTML = '';
      const out = resultsTable(ctx, d.results || []);
      if (typeof out === 'string') resultsEl.innerHTML = out; else resultsEl.appendChild(out);
    } catch (e) {
      resultsEl.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    }
  };

  form.addEventListener('submit', (ev) => { ev.preventDefault(); search(); });
  loadStatus();
  search();
}

/* ------------------------------------------------------------------ admin (plugin settings) panel */

function admin(target, ctx) {
  const root = shell(target);
  let message = null;
  let checkReport = null;

  const draw = async () => {
    let s;
    try {
      s = await call(ctx, 'GET', 'status/');
    } catch (e) {
      root.innerHTML = `<div class="msg err">${esc(e.message)}<br>If the plugin was just activated, restart InvenTree so its URLs and database tables load.</div>`;
      return;
    }
    const custom = s.auth_mode === 'custom';
    let audit = [];
    try { audit = await call(ctx, 'GET', 'audit/?limit=15'); } catch (e) { audit = []; }
    let lastCheck = null;
    let lastSelftest = null;
    try { lastCheck = (await call(ctx, 'GET', 'check/')).last; } catch (e) { lastCheck = null; }
    try { lastSelftest = (await call(ctx, 'GET', 'selftest/')).last; } catch (e) { lastSelftest = null; }
    const mode = s.write_mode || 'dry_run';
    root.innerHTML = `
      ${message ? `<div class="msg ${message.ok ? '' : 'err'}">${esc(message.text)}</div>` : ''}
      <h4>Safety</h4>
      <div class="xr-row">Write mode:
        ${mode === 'live' ? '<span class="badge ok">LIVE — writing to Xero</span>' : mode === 'paused'
          ? '<span class="badge err">PAUSED — nothing is sent</span>' : '<span class="badge info">DRY RUN — nothing is written</span>'}
        ${mode !== 'paused' ? '<button class="danger" data-mode="paused">Pause all writes</button>' : ''}
        ${mode !== 'dry_run' ? '<button data-mode="dry_run">Switch to dry run</button>' : ''}
        ${mode !== 'live' ? '<button class="primary" data-mode="live">Go live…</button>' : ''}
      </div>
      <dl class="xr-grid">
        <dt>Approvals</dt><dd>${s.auto_approve ? '<span class="badge warn">InvenTree may approve</span>' : 'Drafts only — you approve in Xero'}</dd>
        <dt>Delete / void</dt><dd>${s.allow_delete_void ? '<span class="badge warn">allowed</span>' : 'Never automatic — held for you'}</dd>
        <dt>New Xero documents</dt><dd>${s.import_new_documents ? 'imported into InvenTree' : 'not imported (status only)'}</dd>
        <dt>Held for review</dt><dd>${s.held ? `<span class="badge err">${s.held}</span> — see the Xero Orders widget (filter "Held")` : 'none'}</dd>
        <dt>Permissions</dt><dd>${esc((s.required_scopes || []).join(' '))}
          ${(s.missing_scopes || []).length ? `<div class="msg err">Reconnect to grant: ${esc(s.missing_scopes.join(' '))}</div>` : ''}</dd>
      </dl>
      <div class="xr-row">
        ${s.connected ? `<span class="badge ok">Connected</span> <b>${esc(s.tenant_name)}</b>` : '<span class="badge err">Not connected</span>'}
        <span class="muted">${custom ? 'Custom Connection' : 'OAuth 2.0 app'}</span>
      </div>
      <dl class="xr-grid">
        ${!custom ? `<dt>Redirect URI</dt><dd><code>${esc(s.redirect_uri)}</code><div class="muted">Add this exact URI to your Xero app.</div></dd>` : ''}
        <dt>Connected</dt><dd>${fmtDate(s.connected_at)} ${s.connected_by ? 'by ' + esc(s.connected_by) : ''}</dd>
        <dt>Scopes</dt><dd>${esc(s.scopes || '—')}</dd>
        <dt>Schedule</dt><dd>${s.scheduled ? `every ${esc(s.interval_minutes)} min` : 'off'} ·
          POs: ${esc(s.po_direction)} · SOs: ${esc(s.so_direction)} (${esc(s.so_document)})</dd>
        <dt>Last sync</dt><dd>${fmtDate(s.last_sync_finished)} ${s.last_sync_ok === false ? statusBadge('ERROR') : ''}
          <div class="muted">${esc(s.last_sync_message || '')}</div></dd>
      </dl>
      <div class="xr-row">
        ${!s.connected && !custom ? `<a href="${esc(s.connect_url)}"><button class="primary">Connect to Xero</button></a>` : ''}
        ${!s.connected && custom ? '<button class="primary" data-act="connect">Connect</button>' : ''}
        ${s.connected && !custom ? `<a href="${esc(s.connect_url)}"><button>Reconnect / switch organisation</button></a>` : ''}
        ${s.connected ? '<button data-act="test">Test connection</button>' : ''}
        ${s.connected ? '<button class="primary" data-act="sync-all">Sync now</button><button data-act="sync-export">Export only</button><button data-act="sync-import">Import only</button>' : ''}
        ${s.connected ? '<button class="danger" data-act="disconnect">Disconnect</button>' : ''}
      </div>
      <h4>Checks</h4>
      <div class="xr-row">
        ${s.connected ? '<button data-act="check">Check configuration</button>' : ''}
        ${s.connected ? '<button data-act="selftest">Run live self-test (Demo Company only)…</button>' : ''}
        <span class="muted">The configuration check only reads, so it is safe on your real organisation.</span>
      </div>
      ${reportBlock('Last configuration check', checkReport || lastCheck)}
      ${reportBlock('Last live self-test', lastSelftest)}
      <h4>Recent activity</h4>${logTable(s.recent)}
      <h4>Recent writes to Xero</h4>${audit.length ? `<div class="tbl"><table><thead><tr><th>When</th><th>Mode</th><th>Request</th><th>Order</th><th>Result</th></tr></thead><tbody>${
        audit.map((a) => `<tr><td>${fmtDate(a.created)}</td><td>${a.mode === 'live' ? '<span class="badge ok">live</span>' : '<span class="badge info">dry run</span>'}</td>
          <td>${esc(a.method)} ${esc(a.path)}</td><td>${esc(a.inventree_ref)}</td>
          <td>${a.mode === 'dry_run' ? 'not sent' : a.ok ? statusBadge('OK').replace('OK', esc(a.response_status)) : `<span class="badge err">${esc(a.response_status || 'failed')}</span> ${esc(a.error)}`}</td></tr>`).join('')
      }</tbody></table></div>` : '<p class="muted">Nothing written yet.</p>'}
      <p class="muted">Save setting changes above before connecting. Full history: Django admin → Xero sync log and Xero writes (audit).</p>`;
    root.querySelectorAll('button[data-mode]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        const mode = btn.dataset.mode;
        if (mode === 'live' && !window.confirm('Go live? The plugin will start writing to your Xero organisation.\n\nHave you checked the dry-run results?')) return;
        try {
          await call(ctx, 'POST', 'mode/', { mode, confirm: mode === 'live' });
          message = { ok: true, text: `Write mode: ${mode.replace('_', ' ')}` };
        } catch (e) { message = { ok: false, text: e.message }; }
        await draw();
      }));
    });

    root.querySelectorAll('button[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => busy(btn, async () => {
        const act = btn.dataset.act;
        try {
          if (act === 'connect') {
            await call(ctx, 'POST', 'connect-custom/', {});
            message = { ok: true, text: 'Connected to Xero' };
          } else if (act === 'test') {
            const r = await call(ctx, 'POST', 'test/', {});
            message = { ok: true, text: `OK — ${r.organisation} (${r.country}, ${r.base_currency})` };
          } else if (act.startsWith('sync-')) {
            await call(ctx, 'POST', 'sync/', { direction: act.slice(5) });
            message = { ok: true, text: 'Sync queued — results appear in the activity log shortly' };
          } else if (act === 'check') {
            checkReport = await call(ctx, 'POST', 'check/', {});
            message = { ok: checkReport.passed, text: checkReport.passed ? 'Configuration check passed' : 'Configuration check found problems — see below' };
          } else if (act === 'selftest') {
            const typed = window.prompt('The live self-test writes tagged test documents to the connected organisation. '
              + 'It only runs on a Xero Demo Company.\n\nType DEMO to continue:');
            if (typed !== 'DEMO') return;
            await call(ctx, 'POST', 'selftest/', { confirm: 'DEMO' });
            message = { ok: true, text: 'Self-test queued — it takes about a minute; refresh this page for the report' };
          } else if (act === 'disconnect') {
            if (!window.confirm('Disconnect InvenTree from Xero?')) return;
            await call(ctx, 'POST', 'disconnect/', {});
            message = { ok: true, text: 'Disconnected' };
          }
        } catch (e) {
          message = { ok: false, text: e.message };
        }
        await draw();
      }));
    });
  };
  draw();
}

/* ------------------------------------------------------------------ exports */

function mount(a, b, renderer) {
  if (a && a.nodeType === 1) {
    renderer(a, b || {});
    return undefined;
  }
  const ctx = a || {};
  const React = window.React;
  if (!React) {
    console.error('Xero plugin: React is not available and no target element was given');
    return null;
  }
  function XeroHost() {
    const ref = React.useRef(null);
    React.useEffect(() => { if (ref.current) renderer(ref.current, ctx); }, []);
    return React.createElement('div', { ref });
  }
  return React.createElement(XeroHost);
}

export function renderXeroPanel(a, b) { return mount(a, b, panel); }
export function renderXeroDashboard(a, b) { return mount(a, b, dashboard); }
export function renderXeroAdmin(a, b) { return mount(a, b, admin); }
