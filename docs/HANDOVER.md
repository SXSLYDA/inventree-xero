# Maintainer handover

Everything needed to pick this project up cold: what it does, how it's built, why it's built that way, what is still unproven, and how to test and release it.

## Status (October 2026, v1.4.1)

- **Feature complete** for purchase orders, bills, sales invoices/quotes, contacts, balances, items, and the inventree-woocommerce web shop (orders, quote requests, credit).
- **Safe mode** (1.3) on by default: dry run, drafts only, no delete/void, pre-send checks, strict contact matching, idempotency keys, audit, alerts, encrypted tokens, least-privilege scopes.
- **Tested against:**
  - real InvenTree 1.6.0, end to end (157 checks), with the real inventree-woocommerce 0.6.0 installed;
  - unit tests and UI render tests.
- **Not yet run against the real Xero API.** All Xero behaviour has been tested against `tests/fake_xero.py`, which models Xero's documented behaviour. The live self-test (`selftest.py`) exists to close that gap. Run it against a Demo Company before going live (see [IMPLEMENTATION.md](IMPLEMENTATION.md), stage 4) and fix any FAIL it reports.

| Component | Version | Notes |
|---|---|---|
| inventree-xero | 1.4.1 | this repo; version lives in `inventree_xero/version.py` |
| InvenTree | 1.6.0+ | uses line `discount` (1.6), FSM order transitions, 1.6 event names |
| inventree-woocommerce | 0.6.0+ | optional; reads Xero balances for credit; 0.6.0 added the quote provider hooks and part-less quote lines |
| inventree-shop (WordPress) | 0.4.1+ | hides line tick-boxes for whole-quote acceptance |
| Xero API | Accounting 2.0, granular OAuth scopes | apps created after 2 Mar 2026 must use granular scopes |

## Module map

| File | Responsibility |
|---|---|
| `core.py` | Plugin class: all settings, schedule (5-min tick, real interval from settings), event routing, URLs, UI panel registration |
| `xero_client.py` | Xero API client. **Every write goes through `_write_gate`**: paused / dry run / live, idempotency key, audit record. OAuth and custom-connection auth, token refresh with row lock, retries (429/5xx/network), `required_scopes()` |
| `safety.py` | Drafts-only cap, delete/void gate, approved-in-Xero protection, pre-send checks (price, quantity, account, tax, totals). Raises `Hold` |
| `sync.py` | The sync engine: exports (PO, bill, SO), imports (PO, bill, sales, balances), per-order lock, holds, dry-run notes, preview, alerts, logging |
| `mapping.py` | Metadata helpers, Xero document normalisation, line ownership (`preserved_lines`), contact matching (strict), item mapping |
| `shop_quotes.py` | Web shop quote requests ↔ Xero quotes, both directions, using the shop plugin's `apply_action` |
| `checks.py` | Read-only configuration check (safe on a real organisation) |
| `selftest.py` | Live self-test (Demo Company only) |
| `alerts.py` | Daily-deduplicated alert emails via InvenTree's mailer |
| `crypto.py` | Fernet encryption of tokens, key derived from `SECRET_KEY` |
| `views.py` | OAuth views and the JSON API used by the UI (staff-only writes) |
| `models.py` | `XeroConnection` (singleton: tokens, tenant, bookkeeping), `XeroSyncLog`, `XeroQuoteLink`, `XeroWriteAudit` |
| `static/xero_ui.js` | Panels (PO, SO, company, part), dashboard widget, settings panel. Plain JS, InvenTree's legacy `(target, context)` entry convention, no build step |
| `management/commands/` | `xero_check`, `xero_selftest` |

## Where links live

Links are stored in each InvenTree object's `metadata['xero']`, so they travel with the object and need no join tables:

```python
purchase_order.metadata['xero'] = {
    'id', 'doc_type': 'po', 'number', 'status', 'url', 'total', 'amount_due', 'amount_paid', 'currency',
    'updated',            # Xero UpdatedDateUTC last seen (import loop protection)
    'hash',               # hash of the last payload sent (export change detection)
    'line_ids',           # Xero LineItemIDs InvenTree owns; everything else is Xero-only and preserved
    'xero_only': {count, total, lines},
    'origin': 'inventree' | 'xero', 'lines_sig', 'final', 'error', 'held': {reason, at}, 'dry_run': {would, at},
    'bill': { same shape, plus 'date' },
}
sales_order.metadata['xero']   # same, doc_type 'invoice' | 'quote'; plus 'payment_recorded', 'shop_order', 'quote'
company.metadata['xero']       # contact_id, contact_name, url, contact_hash, balance{receivable_*, payable_*, updated}
part.metadata['xero']          # item_id, item_code, url
```

Shop quote links live in `XeroQuoteLink` (keyed by the shop's `QuoteRequest` id) so the shop plugin needs no changes.

## How each flow works

**Export (InvenTree → Xero)**
1. `plan_*` builds the payload. It returns `None` with a reason when nothing should be sent, or raises `Hold`.
2. If the hash is unchanged, nothing happens.
3. Otherwise `guarded_send`:
   1. checks approved-in-Xero protection;
   2. runs the pre-send checks;
   3. calls `_send`, which re-reads the Xero document and appends lines InvenTree doesn't own;
   4. posts through the write gate.
4. The link is stored.

**Import (Xero → InvenTree)**
- Incremental by `If-Modified-Since` per document type (`XeroConnection.import_marks`).
- Linked documents: status mapped back (place/issue/cancel through InvenTree's FSM transitions). Line refresh happens only for Xero-originated orders still pending.
- New documents are created only with *Import new Xero documents* on, and only if they contain part lines.
- After an import the plugin records the export hash, so nothing bounces back.

**Bills**
- Built from **received** quantities, plus the PO's extra lines.
- Draft while the PO is open. The status cap applies at completion.
- The Xero PO is marked Billed when complete.
- Bills keyed in Xero are linked by PO reference and never edited.

**Web shop orders**
- Event `woocommerce.salesorder_created` → export now.
- Paid online:
  - approved immediately only with *Let InvenTree approve*;
  - the payment is recorded only with that setting and a payment account.

**Shop quotes**
- Shop status → Xero status mapping, with placeholder lines for unpriced requests.
- Xero edits go back through `apply_action`, so they get the shop's own checks and customer emails.
- A revised sent quote returns to Draft. Marking it Sent sends it.
- *Price and send in Xero*: this plugin is the shop's **quote pricing provider** (hooks `woocommerce_quote_provider/_info/_pdf` on `XeroPlugin`, logic in `shop_quotes.provider_*`). The shop locks its own pricing and serves Xero's PDF (`XeroClient.get_pdf`, falling back to the shop's PDF if Xero fails). Lines are written to Xero only on create (placeholders); afterwards `build_payload` sends status/expiry/contact only, so discounts, tax rates and accounts set in Xero survive.
- Xero → shop lines (`rows_from_xero`): salable part codes → part lines, everything else → charges (no part). Prices = `LineAmount` (after discount) minus `TaxAmount` for Inclusive quotes. Zero-priced placeholders/part lines block; zero notes are skipped.
- The provider is inactive in a dry run or when disconnected, so the shop falls back to pricing in InvenTree rather than getting stuck.

**Balances**
- Receivable/payable outstanding and overdue per linked company, refreshed hourly (new links immediately).
- Stored in metadata and an optional parameter; event `xero.balances_updated` is fired.
- inventree-woocommerce 0.4.1 uses this for credit.

## Design decisions (and why)

| Decision | Why |
|---|---|
| **InvenTree proposes, Xero decides** (drafts only, no delete/void by default) | Approved documents hit the ledger and BAS; a person stays in the loop |
| **InvenTree holds parts only**; Xero-only lines preserved via `line_ids` | Labour, freight and interest belong in Xero; InvenTree must never strip them |
| Links in **metadata**, not join tables | Survive InvenTree upgrades, visible in the API, no migrations for links |
| **Hash** of payload for change detection | InvenTree orders have no reliable "modified" timestamp |
| **Holds** instead of errors for anything a person should decide | Errors are for things going wrong; holds are for things needing judgement, and they clear themselves when fixed |
| **Strict contact matching** | A wrong contact means invoices to the wrong customer, the worst kind of silent error |
| **Idempotency keys**: per-day for creates, per-request for updates | Creates can't duplicate even across runs; repeated identical updates (quote Sent→Draft→Sent) stay possible |
| **Dry run is the default** | Safe on install and on upgrade |
| Totals check uses `calculate_total_price()`, not the stored `total_price` | The stored total can lag behind line edits |
| Shop integration through the shop's **public pieces only** (models, `apply_action`, events) | Keeps the two plugins independently upgradeable |
| Polling with `If-Modified-Since`, not Xero webhooks | No public endpoint or webhook key needed; adequate at small-business volume. Webhooks are a possible future addition |

## Assumptions still to confirm against real Xero

Covered by the **live self-test** (each is a named step in its report):
- PO/invoice/bill/quote creation as Draft; status transitions, including **quote Sent → Draft** and Accepted → Invoiced, and PO refusing to move backwards
- quote PDF download (`Accept: application/pdf`)
- line replacement on update, and Xero-only line preservation via `LineItemID`
- `DiscountRate` honoured on PO, invoice and bill lines
- `Idempotency-Key` honoured (no duplicate on a repeated create)
- `If-Modified-Since` on list endpoints
- payments (if the payments permission is granted), void and delete
- contact `searchTerm`, contact detail updates, `TaxNumber` kept
- item create/list/delete

Checked by the **configuration check**:
- account codes and tax types exist and suit their use
- payment account can take payments
- whether contact lists include `Balances`
- organisation lock date

Still manual / unverified:
- **"Open in Xero" deep-link paths.** The self-test lists them for you to click.
- Granular scope names `accounting.settings.read` / `accounting.contacts.read` for apps created after March 2026. If Xero rejects one at connect time, set the *OAuth scopes* setting by hand.
- How long Xero retains idempotency keys (the per-day create key assumes at least minutes to hours).
- Rate-limit header names (`Retry-After`, `X-Rate-Limit-Problem`).
- Per-line `TaxAmount` on tax-inclusive quotes as the fake models it (line amount × rate / (100 + rate)). Price one Inclusive quote with a discount and compare the shop's ex-tax line with Xero's before relying on it.

When the self-test reports a FAIL, fix the plugin, add the real behaviour to `tests/fake_xero.py` so the fake stays honest, and re-run.

## Not built (ideas for later)

- Credit notes; partial payments shown line by line; tracking categories; attachments to Xero
- InvenTree return orders → Xero credit notes
- Item price sync (InvenTree prices → Xero item prices)
- Xero webhooks instead of polling
- Multiple Xero organisations per InvenTree
- Carrier/postage lines from the shop (the shop plugin doesn't produce them yet)

## Tests

| Suite | What | Run |
|---|---|---|
| Unit | pure helpers, safety rules, crypto, scopes, idempotency keys | `pip install -e . pytest && pytest tests/test_mapping.py tests/test_safety.py` |
| UI | renders every panel in jsdom with InvenTree's legacy entry convention | `npm install --no-save jsdom@24 && node --test tests/ui.test.mjs` |
| End to end | real InvenTree DB + real inventree-woocommerce + fake Xero; every flow, safety rule, check and the self-test (157 checks) | see below |
| Settings doc | docs match the code | `python tools/gen_settings_doc.py --check` |
| Live | the real Xero API | the self-test, from the settings panel or `inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py xero_selftest --confirm-demo` |

**Running the end-to-end test locally** (what CI does):

```bash
git clone --depth 1 --branch 1.6.0 https://github.com/inventree/InvenTree.git inventree
python -m venv venv && . venv/bin/activate
pip install -r inventree/src/backend/requirements.txt
pip install -e /path/to/inventree-xero
pip install "inventree-woocommerce @ git+https://github.com/SXSLYDA/inventree-woocommerce.git"
export INVENTREE_DB_ENGINE=sqlite3 INVENTREE_DB_NAME=$PWD/test.sqlite3 INVENTREE_MEDIA_ROOT=$PWD/media \
       INVENTREE_STATIC_ROOT=$PWD/static INVENTREE_SECRET_KEY=test-secret-key-test-secret-key \
       INVENTREE_PLUGINS_ENABLED=True INVENTREE_PLUGIN_TESTING=True INVENTREE_PLUGIN_TESTING_SETUP=True \
       INVENTREE_SITE_URL=https://inventree.example.com XERO_TESTS=/path/to/inventree-xero/tests
cd inventree/src/backend/InvenTree
python manage.py migrate --noinput
python manage.py shell -c "from plugin.models import PluginConfig; [PluginConfig.objects.update_or_create(key=k, defaults={'active': True}) for k in ('xero', 'woocommerce')]"
python manage.py migrate
python manage.py shell -c "exec(open('/path/to/inventree-xero/tests/e2e_inventree.py').read())"
```

Use a fresh database (and empty media folder) for each run. The test writes orders and settings.

**How the fake works.** `FakeXeroClient` replaces only the HTTP layer (`_http`). The real client code runs on top of it: dry run, audit, idempotency, retries. It models:
- IDs and LineItemIDs;
- line replacement;
- PO and quote status rules;
- payments;
- `If-Modified-Since`;
- idempotency-key caching;
- lost responses (`drop_after_commit`);
- organisation, accounts and tax rates.

A mutation check confirmed the idempotency test fails if keys aren't honoured.

## Releasing

The version lives in one place, `inventree_xero/version.py`; `pyproject.toml` reads it from there.

**While you work:** add a line under `## Unreleased` in `CHANGELOG.md` for anything a user would notice. Mention new migrations and anything that changes behaviour.

**To release** (PyCharm's terminal, in the repo root):

```powershell
.\build-and-bump.ps1                 # patch: 1.4.1 -> 1.4.2
.\build-and-bump.ps1 -Bump minor     # new features: 1.4.1 -> 1.5.0
.\build-and-bump.ps1 -Version 2.0.0  # exact version
```

1. **Checks:** git is available; the tag doesn't exist yet locally or on GitHub; the new version is higher.
2. **Build:** regenerates `docs/SETTINGS.md` from the code.
3. **Tests:** lint plus the unit tests, and a syntax check of the UI file if Node is installed. Your venv needs `pip install -e . pytest pyflakes` once.
4. **Bump:** writes `version.py`; turns `## Unreleased` into `## X.Y.Z — date` and starts a fresh empty Unreleased section. It asks before releasing with no notes (`-Yes` skips the question).
5. **Commit and tag:** commits all changes as `Release vX.Y.Z` and adds an annotated tag `vX.Y.Z`. It does not push. To undo before pushing: `git tag -d vX.Y.Z; git reset --soft HEAD~1`.

It stops at the first problem, before changing anything. Then:

6. **Push the commit and the tag.**
   - PyCharm: *Git → Push* with **Push tags** ticked. It's off by default, and an unpushed tag can't be installed.
   - Terminal: `git push --follow-tags`.
7. **CI runs on the tag:**
   - unit, UI and end-to-end tests;
   - a check that the tag matches `version.py` and that `CHANGELOG.md` has the section.
8. **Update the server:** `sudo inventree-xero-update vX.Y.Z` (or just `sudo inventree-xero-update` for the newest), or let CI deploy it (below).

**Why tags and not "latest from main":**
- The server's `plugins.txt` pins the plugin to a tag. A new tag is a new install link, so `invoke update` (which runs `pip install -U -r plugins.txt`) really installs it.
- Only releases you deliberately tagged and that passed CI can reach the server.

The OMG plugin's approach (bump on every build, install from `main`) is fine for an internal tool, but not for something that writes to the accounts.

**Optional: deploy from GitHub.** The `deploy` job in `ci.yml` runs only on release tags, only after every test passes, and only when you switch it on:
1. Settings → Secrets and variables → Actions.
   - **Variable:** `XERO_DEPLOY` = `true`.
   - **Secrets:**
     - `INVENTREE_HOST`, `INVENTREE_SSH_USER`, `INVENTREE_SSH_KEY` (a private key whose public half is in the server user's `~/.ssh/authorized_keys`);
     - `INVENTREE_SSH_KNOWN_HOSTS` (the output of `ssh-keyscan inventree.omgharness.com`).
   - These are the same secret names as the OMG plugin's deploy.
2. Settings → Environments → **inventree-production** → *Required reviewers*: add yourself. Each deploy then waits for you to click **Approve**. Recommended.
3. The SSH user must be able to `sudo` without a password prompt, or be `root`.

The job sends `tools/server-update.sh` over SSH and runs it with the tag, so a GitHub deploy does exactly what a manual update does: backup, pin, update, restart, version check, configuration check.

**Migrations:** add a new numbered migration for any model change, and never edit a released one. Mention it in the changelog, because it decides whether rolling back is safe.

## Picking this up with an AI assistant later

Give it:
1. this repo (or zip);
2. `docs/HANDOVER.md` (this file);
3. the latest **self-test report** and **configuration check** text from the settings panel;
4. your InvenTree version;
5. any held reasons or sync-log errors you're seeing.

That's enough to reproduce the context without the original conversation.
