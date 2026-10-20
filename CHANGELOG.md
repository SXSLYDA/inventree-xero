# Changelog

Add notes for the next release under **Unreleased** as you work. `build-and-bump.ps1` turns that heading into the version number and date when you release.

## Unreleased

## 1.4.1 — 2026-10-20

- **Release tooling:**
  - `build-and-bump.ps1` (Windows/PyCharm): checks, tests, bumps the version, stamps this changelog, commits and tags `vX.Y.Z`.
  - `tools/server-update.sh`: moves the server's `plugins.txt` pin to a release tag, backs up, updates and restarts InvenTree, and confirms the installed version.
  - CI checks that the tag matches the version, and can deploy release tags to your server (opt-in).
- The version now lives only in `inventree_xero/version.py`; `pyproject.toml` reads it from there.
- Docs: command-line checks on package installs are `inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py …`.

## 1.4.0 — 2026-10-20

- **Live self-test** against a Xero Demo Company (refuses any real organisation): contacts, items, purchase orders, bills, invoices, payments, quotes and their status rules, discounts, tax types, keeping Xero-only lines, idempotency and If-Modified-Since — with a pass/fail report, tagged test data and clean-up. Settings panel button (type DEMO to confirm) and `manage.py xero_selftest --confirm-demo`.
- **Configuration check** (read-only, safe on a real organisation): connection, demo vs real, lock date, permissions, account codes and tax types exist and suit their use, shop payment account, currency, contact balances, write/approval settings, alert recipients, email and background worker. Settings panel button and `manage.py xero_check`.
- **Fix:** idempotency keys for *updates* are now unique per request (still reused across that request's retries). In 1.3.0 an identical update repeated later — such as a quote going Sent → Draft → Sent — would have been ignored by Xero. Creates keep their per-day key, so a lost reply can still never create a duplicate.
- **Documentation:** `docs/IMPLEMENTATION.md` (step-by-step go-live guide with checks, rollback and troubleshooting), `docs/SETTINGS.md` (generated from the code; CI checks it), `docs/HANDOVER.md` (architecture, decisions, unverified assumptions, tests, releasing).

## 1.3.0 — 2026-10-20

Safe mode: built to fail safe because it writes to your accounts.

- **Write mode**: *Dry run* (default: logs everything it would send, writes nothing), *Live*, *Paused* (kill switch for the sync, events and manual pushes). Switchable from the settings panel; going live needs confirmation.
- **Drafts only** (default): purchase orders, invoices and bills are never approved by InvenTree; once approved in Xero, InvenTree changes are held instead of written over the approved document.
- **No delete / void** (default): cancellations are held for a person.
- **Checks before sending**: unpriced or zero-quantity lines, missing account code or tax type, and lines that don't add up to the InvenTree order total hold the document. Held documents show the reason on the order panel, appear under *Held for review*, and clear themselves once fixed.
- **Strict contact matching** (default): duplicates, near-matches and email/ABN conflicts are held; link by hand from the company panel's new *Find in Xero*.
- **Idempotency keys** on every write: a retry after a dropped connection can't create a duplicate.
- **Write audit**: every live and dry-run write recorded with request and response (new `XeroWriteAudit` table, migration 0003, admin page, `audit/` API, recent writes on the settings panel).
- **Email alerts** for errors and held documents (deduplicated daily).
- **Encrypted tokens** at rest; existing tokens encrypted on upgrade.
- **Least-privilege scopes** worked out from the enabled features; the settings panel lists any the connection is missing.
- **Staff-only** writes in the API.
- **Preview** button for orders and bills.
- New Xero documents are no longer imported unless *Import new Xero documents* is on; payments are only recorded with *Let InvenTree approve*.
- Revised shop quotes go back to Draft in Xero and wait to be marked Sent (*Re-send revised quotes automatically* is off by default).
- Upgrading: write mode starts as **Dry run**; switch to Live when ready.

## 1.2.1 — 2026-10-20

- Verified end to end against the real inventree-woocommerce 0.4.0/0.4.1 plugin (quote requests, web orders, credit); no plugin code changes were needed.
- Test stand-in for the shop plugin removed; CI installs the real plugin.
- inventree-woocommerce 0.4.1 uses this plugin's Xero balances for account credit.

## 1.2.0 — 2026-10-20

First public release.

- **Web shop quote requests ↔ Xero quotes**: requests appear as Xero draft quotes (placeholder line per item); pricing done in Xero is copied to the shop quote; marking the Xero quote Sent makes the shop send it to the customer; edits to a sent quote reopen, update and re-send it; Draft reopens it; Declined (with the Summary as the reason) declines it; shop decline/expiry/acceptance flow back to Xero; accepted quotes are marked Invoiced once their sales order is invoiced.
- Setting *Xero document for web orders*: invoice (default) or follow the sales order setting.
- Settings for quote mode, Xero edits, a service part for non-part quote lines, and a default decline reason.
- New `XeroQuoteLink` table (migration 0002) and admin page; quotes API; *Web shop quotes* view in the dashboard; sales order panel shows the originating quote.
- A blocked Xero edit (e.g. a line without a part) is retried once fixed instead of being treated as synced.

## 1.1.0 — 2026-10-20

- **Bills**: create Xero bills from received purchase order lines (as received, or on completion), keep the draft up to date, mark the Xero PO billed, link bills entered in Xero, show bill payment status.
- **InvenTree holds parts only**: Xero lines that aren't parts (labour, freight, interest) are never imported and are preserved when InvenTree updates a Xero document; documents with no part lines are skipped; Xero-only lines are listed on the order.
- **Web shop**: works with inventree-woocommerce — web orders invoiced on arrival, paid-online invoices approved and the payment recorded against a clearing account.
- **Contacts**: link-only / create / create-and-update modes; InvenTree detail changes pushed to Xero; tax number sent; receivable and payable balances imported per company (metadata + optional parameter) with an `xero.balances_updated` event.
- **Items**: parts linked to Xero items (metadata), part panel to link or create items.
- Line discounts sent and imported (Xero `DiscountRate`).
- Event names updated for InvenTree 1.6 (`salesorder.hold`, `salesordershipment.completed`, `purchaseorderitem.received`).
- Per-order lock so events and the schedule can't post the same order twice.
- Order transitions use InvenTree's state machine; invalid transitions are skipped.
- Each sync step runs independently — one failing step no longer stops the rest.
- Requires InvenTree 1.6.
- Tests: unit tests, jsdom UI tests, end-to-end test against a real InvenTree with a fake Xero organisation; GitHub Actions CI.

## 1.0.0 — 2026-10-20

- Purchase order and sales order (invoice or quote) two-way sync, contact and item matching, schedule, status-change pushes, search dashboard, order and company panels, admin panel.
