# inventree-xero

An [InvenTree](https://inventree.org) plugin that keeps InvenTree and [Xero](https://www.xero.com) in step:

| InvenTree | Xero | |
|---|---|---|
| Purchase Order | Purchase Order | export + import |
| Received purchase order lines | Bill (accounts payable) | export, payment status back |
| Sales Order | Sales Invoice, or Quote | export + import, payment status back |
| Web shop quote request | Quote | both ways: price, send, revise and decline from Xero |
| Company (supplier / customer) | Contact | link / create / update; balances back |
| Part / Supplier Part | Item | matched by part name, IPN or SKU; optionally created |

Every feature can be switched on or off in the plugin settings.

**InvenTree holds parts; Xero holds the money.**
- Lines in Xero that aren't parts stay in Xero. Labour, freight, account interest, call-out fees and so on are never imported into InvenTree.
- Those lines are preserved whenever InvenTree updates the Xero document. See [Lines that live only in Xero](#lines-that-live-only-in-xero).

Requires **InvenTree 1.6** or later. Tested end to end against InvenTree 1.6.0, with inventree-woocommerce 0.6.0 (quote pricing in Xero needs 0.6.0+). Before going live, prove it against your own Xero Demo Company with the built-in live self-test.

---

**Documentation:** [Implementation guide](docs/IMPLEMENTATION.md) (step by step to going live) · [Settings reference](docs/SETTINGS.md) · [Maintainer handover](docs/HANDOVER.md) · [Changelog](CHANGELOG.md)

## Safety

This plugin writes to your accounting system, so it is built to fail safe. Out of the box:

| Rule | Default | What it means |
|---|---|---|
| **Write mode** | **Dry run** | Works out and logs everything it *would* send to Xero, and writes nothing. Switch to *Live* when you're happy. *Paused* is the kill switch: nothing is sent or changed. |
| **Drafts only** | On | Purchase orders, invoices and bills always arrive in Xero as **Draft**. A person approves them in Xero. Once something is approved in Xero, later InvenTree changes are *held*, never written over it. |
| **No delete / void** | On | Cancelling in InvenTree never deletes or voids a Xero document. It is held for you to deal with in Xero. |
| **Checks before sending** | On | A document is held if a line has no price or quantity, the account code or tax type isn't set, or its lines don't add up to the InvenTree order total. |
| **Strict contact matching** | On | A contact is only linked automatically on an exact name with no conflicting email or ABN. Duplicates and near-matches ("Acme Cables" vs "Acme Cables Pty Ltd") are held for you to link by hand on the company's Xero panel. |
| **New Xero documents** | Not imported | Only the status of linked documents comes back from Xero (approved, paid, voided). Nothing is created in InvenTree from Xero unless you switch it on. |
| **Payments** | Not recorded | Let your Stripe / bank feed reconcile payments in Xero. Recording them needs *Let InvenTree approve* and a payment account. |
| **Quotes to customers** | Only when you mark them Sent | A quote revised in Xero goes back to Draft. Marking it Sent again is the deliberate step that sends it. |

Always on:
- **No duplicates on retries:** every write carries an `Idempotency-Key`, so a retry after a dropped connection can't create a second document.
- **Full audit:** every write and every dry-run write is recorded with exactly what was sent and what Xero replied (Django admin → *Xero writes (audit)*).
- **Alerts:** errors and held documents are emailed to *Alert emails* (default: superusers), at most once a day per problem.
- **Encrypted tokens:** the Xero tokens are encrypted at rest with a key derived from InvenTree's `SECRET_KEY`.
- **Least privilege:** only the Xero permissions the switched-on features need are requested.
- **Staff only:** only staff can push, link, sync or change the write mode; anyone with order access can view.

Every order panel has a **Preview** button: the exact lines that would be sent, and anything that would hold them.

Two built-in checks:
- **Check configuration** only reads, so it's safe on your real organisation. It confirms:
  - the connection and permissions;
  - your account codes and tax types exist and suit their use;
  - the payment account, currency and lock date;
  - the background worker and email.
- **Run live self-test** only runs on a Xero **Demo Company**. It exercises every Xero behaviour the plugin relies on with tagged test documents and gives a pass/fail report.

Both are on the settings panel, and on the command line:
- `sudo inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py xero_check`
- `sudo inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py xero_selftest --confirm-demo`

### Going live — recommended order

The [implementation guide](docs/IMPLEMENTATION.md) walks through this with checks at every step.

1. Connect the plugin to **Xero's free Demo Company** first. Run **Check configuration** and the **live self-test**, try your own flows in *Live*, then disconnect.
2. Connect your real organisation in **Dry run**. Let it run for a week or two and read the log and previews. Fix anything held.
3. Go *Live* with **purchase orders** only (everything arrives as Draft).
4. Add **bills**, then **sales invoices**, then **web shop quotes**, each after a quiet week.
5. Only then consider *Let InvenTree approve* or *Allow delete / void*, and only if you really want them.

**Upgrading from 1.2:** settings you saved keep their values, but the new safety settings start at their safe defaults. **Write mode starts as Dry run, so nothing is written until you switch it to Live.** Tokens saved by 1.2 are encrypted automatically. Reconnect if the settings page lists missing permissions.

## Contents

- [Safety](#safety)
- [Install](#install)
- [Create the Xero app](#create-the-xero-app)
- [Configure and connect](#configure-and-connect)
- [How it syncs](#how-it-syncs)
- [Lines that live only in Xero](#lines-that-live-only-in-xero)
- [Bills](#bills)
- [Web shop (inventree-woocommerce)](#web-shop-inventree-woocommerce)
- [In the InvenTree UI](#in-the-inventree-ui)
- [Settings reference](#settings-reference)
- [For other plugins](#for-other-plugins)
- [API](#api)
- [Development](#development)

## Install

**Package installer:** use the update script. It installs the newest release, pinned to its tag, and does the same for every later update:

```bash
sudo curl -fsSL -o /usr/local/bin/inventree-xero-update \
     https://raw.githubusercontent.com/SXSLYDA/inventree-xero/main/tools/server-update.sh
sudo chmod +x /usr/local/bin/inventree-xero-update
sudo inventree-xero-update            # newest release; or: sudo inventree-xero-update v1.4.1
```

It backs up the database, points the `inventree-xero` line in `plugins.txt` at the release tag (`inventree-xero @ git+https://github.com/SXSLYDA/inventree-xero.git@vX.Y.Z`), runs `invoke update` (install, migrations, static files), restarts InvenTree, confirms the installed version and runs the configuration check. `sudo inventree-xero-update --check-only` shows what's installed and what's newest without changing anything.

**Docker:** add `inventree-xero @ git+https://github.com/SXSLYDA/inventree-xero.git@vX.Y.Z` (the newest release tag) to `plugins.txt` in your data volume, then run `invoke update` and restart the containers. To update, change the tag and repeat.

**Manual / development:** run `pip install -e .` inside InvenTree's virtualenv, then `invoke migrate` and `invoke static`.

Then go to **Admin Center → Plugin settings** and enable:
- URL integration
- Schedule integration
- Event integration
- App integration
- Interface integration

Restart InvenTree, then activate **Xero** in *Admin Center → Plugins*. The plugin keeps its own two tables (the connection and the sync log), so `invoke migrate` must have run.

The background worker must be running. The schedule and all syncs happen there.

## Create the Xero app

At **developer.xero.com → My Apps → New app**, choose one:

- **Web app (OAuth 2.0, free).**
  - Redirect URI: `https://<your-inventree>/plugin/xero/oauth/callback/`. The exact URI is shown in the plugin's settings panel.
  - Copy the Client ID and generate a Client secret.
- **Custom Connection** (paid Xero add-on, no browser login).
  - Pick the scopes listed below and authorise it for your organisation.

Scopes are Xero's granular scopes. Apps created after 2 March 2026 cannot use the old `accounting.transactions`.

| Scope | Needed for |
|---|---|
| `accounting.invoices` | purchase orders, invoices, bills, quotes, items |
| `accounting.contacts` | contacts and balances |
| `accounting.settings` | organisation details, items |
| `accounting.payments` | only to record web shop payments (remove it from the scope setting if unused) |
| `offline_access` | OAuth apps: lets the scheduled sync refresh its token |

## Configure and connect

1. Open the plugin's settings and fill in:
   - Client ID and secret
   - InvenTree public URL
   - account codes and tax types: the purchase and sales account codes, and tax types for your country. AU example: `INPUT` / `OUTPUT`. Blank uses each account's default.
2. Save. In the **Xero** panel on the same page, click **Connect to Xero** (or **Connect** for a Custom Connection), then **Test connection**.
3. Choose which features you want. Everything below has its own setting.

The schedule checks every 5 minutes and syncs when **Sync interval** has passed. Status changes are also pushed immediately: order placed / issued / shipped / completed / cancelled, and stock received when bills are on.

## How it syncs

**Purchase orders → Xero**
- POs in *PO statuses to export* are created in Xero:
  - Pending POs go in as Draft.
  - Placed POs get *Xero status for placed POs*.
  - Cancelled POs are deleted.
- The Xero PO number is the InvenTree reference.
- Lines carry:
  - the supplier SKU, part name, description and MPN;
  - the line discount;
  - the Xero item code (when the item exists or *Create Xero items* is on).
- Only changed orders are sent: a hash of what was sent is kept.
- A Xero PO never moves backwards; Xero doesn't allow it.

**Sales orders → Xero**
- Become draft sales invoices. They are approved when *Approve invoice when* is reached.
- Cancelled orders are deleted (drafts) or voided (unpaid approved invoices).
- Quote mode follows Draft → Sent → Accepted, then Declined/Deleted on cancel.

**Xero → InvenTree**
- Incremental: only documents changed since the last run.
- New Xero POs, invoices or quotes in the configured statuses become InvenTree orders, but only if they have part lines.
  - The supplier or customer is matched by name or created.
  - Part lines are matched by item code. The rest stays in Xero.
- Linked orders get Xero's status:
  - approved → place / issue
  - deleted / voided → cancel
  - paid → shown on the order
- Orders that started in InvenTree keep InvenTree as master for their part lines.
- Orders that started in Xero refresh their part lines while still pending.

**Loop protection.** After each import the plugin records what an export would send, so nothing bounces straight back. A per-order lock stops an event and the schedule from posting the same order twice.

## Lines that live only in Xero

Each link stores the Xero line IDs that InvenTree owns. Everything else on the Xero document is Xero's.

- **Import** — only part lines come into InvenTree. Change *Xero lines that are not parts* to import them as extra lines instead.
  - With *Skip Xero documents without parts* on, a labour-only, interest-only or freight-only document never creates an InvenTree order.
- **Update** — when InvenTree changes an order, the plugin re-reads the Xero document and sends its own lines plus the untouched Xero-only lines. Freight added to a bill in Xero, or labour added to an invoice, survives.
- **Cancel** — if a Xero document has Xero-only lines, cancelling the InvenTree order does *not* delete or void it. You get a warning to deal with it in Xero, since the labour or freight may still be billable.
- **Visibility** — the order's Xero panel lists the Xero-only lines and their total. That explains why the InvenTree total and the Xero total differ.

## Bills

*Create bills*: **Off**, **As stock is received**, or **When the purchase order is completed**.

- The bill (ACCPAY) uses received quantities × purchase price, plus the PO's extra lines.
- While the PO is open, the bill stays Draft and follows each receipt.
  - Add the supplier's invoice number and their freight in Xero; those lines are kept.
- When the PO completes, the bill gets *Bill status when PO complete* (Draft by default), and the Xero PO is marked **Billed**.
- After the bill is approved in Xero, InvenTree stops changing it. Payments still come back to the PO panel.
- *Link bills entered in Xero*: a bill keyed in Xero whose reference contains an InvenTree PO number is linked to that PO. InvenTree shows it but never edits it.
- The bill's **Reference** in Xero is the PO number and supplier reference (sent as the bill's `InvoiceNumber`, which is what Xero shows as a bill's Reference).

### One bill per supplier invoice

A supplier that ships an order in several boxes sends an invoice with each, on its own date. When a purchase order carries `metadata['supplier_invoices']` (the Mouser plugin, [inventree-mouser](https://github.com/SXSLYDA/inventree-mouser), sets it on every Mouser order and fills it as each box is received by its invoice), the order is billed **per supplier invoice** instead of once:

- one ACCPAY bill per invoice, sent once that invoice is recorded (received) - with *Create bills* set to either "as received" or "when completed"; Off is still off;
- dated the invoice date, due on the invoice's due date (else *Bill due (days)*), and the supplier's invoice number as the bill's Reference;
- only the lines and quantities on that invoice, at the purchase order's prices, plus the fees recorded with that invoice (not the PO's extra lines); tax from *Purchase tax type*, so GST is on each bill;
- each bill's status, payments and holds are tracked on its own and shown in a table on the PO's Xero panel; one that's held or fails doesn't stop the others;
- the Xero PO is marked Billed once the PO is complete and every invoice has its bill;
- stock received outside an invoice (e.g. with Receive Items) isn't billed - the panel lists it; a bill keyed in Xero with the supplier's invoice number is linked to that invoice (*Link bills entered in Xero*);
- an order that already has a whole-order bill isn't billed per invoice as well - the panel says so.

## Web shop (inventree-woocommerce)

When the [inventree-woocommerce](https://github.com/SXSLYDA/inventree-woocommerce) plugin is also installed (*Web shop integration* on):

- Each new web order is invoiced in Xero as soon as the shop plugin creates its Sales Order (event `woocommerce.salesorder_created`).
- **Paid online:** the invoice is approved immediately. With *Web shop payment account* set (e.g. your Stripe clearing account), the payment is recorded against it.
  - The payment reference is the payment method and transaction id.
  - A shop/Xero total mismatch (tax or rounding) is recorded and flagged in the log.
- **Charged to account:** the invoice follows the normal *Approve invoice when* rule. The customer's PO number is in the reference.
- Web orders are invoices by default: a web order only exists once the customer has paid or charged it to account. Set *Xero document for web orders* to follow the sales order setting instead.
- The Xero panel and the search show the web order number.

### Quote requests

The shop's quote requests (custom harnesses, copies, components) live in Xero as quotes. The shop still does the talking to the customer: it emails the quote, shows it in their account, takes acceptance and payment, and expires quotes nobody accepts.

| In the shop | In Xero |
|---|---|
| New request (new / waiting for sample / call / reviewing) | **Draft** quote, one placeholder line per requested item |
| Quote sent to the customer | **Sent**, expiry = the quote's *valid until* |
| Accepted (paid or charged to account) | **Accepted**, then **Invoiced** once its sales order is invoiced |
| Declined / expired / cancelled | **Declined** |

*Web shop quote requests* picks where quotes are priced:

| Setting | Priced in | Customer's PDF | Lines written to Xero |
|---|---|---|---|
| **Price and send in Xero** (default) | Xero - the shop's pricing is locked | Xero's | Only the placeholders, once |
| Priced in InvenTree, draft in Xero | the shop's Quote Requests widget (or Xero) | InvenTree's | Every change |
| Priced in InvenTree, into Xero once sent | the shop's widget | InvenTree's | Every change after sending |

**Price and send in Xero.** Xero is the quote; the shop is how the customer gets it.
1. Each request arrives in Xero as a Draft with one `Item 1:` placeholder line per requested item.
2. Price it in Xero the way you price anything: item codes, discounts, tax-inclusive or exclusive, your own account codes and tax rates. Add labour, freight, a call-out fee - any line.
3. Keep the `Item 1:` prefix to tie a line to the customer's item, and add a lead time as ` (lead time 6 weeks)` at the end of the description (both optional).
4. **Mark the quote Sent in Xero.** The shop emails the customer with Xero's PDF attached; the validity comes from the Xero expiry date.
5. The customer accepts the whole quote online and pays or charges it to account. (*Customers can accept part of a Xero quote* allows ticking lines instead.)

How Xero lines reach the shop:
- An item code that's a salable InvenTree part → a part line (Sales Order line, stock and build orders as usual). A code that's an InvenTree part but not Salable stops the update until you fix it.
- Anything else (labour, freight...) → a charge: a fee on the WooCommerce order, an extra line on the Sales Order.
- Prices are what Xero invoices: after discount, ex tax (a tax-inclusive quote has its tax taken off). Part quantities must be whole; fractional charges (1.5 hours) become one line for the amount, with the hours in the description.
- A placeholder or part line still at zero stops the update (it's shown on the quote and retried). A no-charge note line ("Free delivery") stays on the Xero PDF only.
- After the quote is created, nothing is written over its lines: only status, expiry and contact go back to Xero.

The shop's Quote Requests widget shows "Priced in Xero" with the quote number and a link, and keeps samples, calls, notes and declining. In a dry run, or when Xero isn't connected, quotes are priced in the shop until it's back (nothing from Xero could reach the shop meanwhile).

**Priced in InvenTree.** Price and send in the shop's widget and Xero follows. With *Xero quote edits go to the shop* on, Xero edits flow back the same way as above.

**What if the customer doesn't approve?**
- **They want changes:** edit the quote in Xero, even after it's been sent. The shop reopens the quote, which cancels the customer's pending payment link, and updates the lines. The Xero quote goes back to Draft; mark it Sent again to send the revision (or turn on *Re-send revised quotes automatically*). Marking it Draft in Xero just reopens it.
- **You decline it:** mark it Declined in Xero with the reason in the quote's *Summary*; the customer is told that reason. Or decline it in the shop, and Xero follows.
- **They never answer:** the shop reminds them before the quote expires, then expires it. The Xero quote becomes Declined.
- **They accept only some lines** (only when partial acceptance is allowed): the sales order and invoice carry just those lines. The Xero quote is marked Accepted, then Invoiced.

**Other accounting plugins.** The shop finds its pricing provider through three duck-typed hooks (`woocommerce_quote_provider`, `woocommerce_quote_info`, `woocommerce_quote_pdf`) - see inventree-woocommerce's `providers.py`. This plugin's are in `core.py` and `shop_quotes.py`; copy them to make another accounting system the quote.

**Credit checks.** With *Import account balances* on, each linked customer's Xero receivables are stored on the company: outstanding and overdue. That data is described under [For other plugins](#for-other-plugins). It's also written to the company parameter *Balance parameter* (default `Xero Outstanding`).

inventree-woocommerce 0.4.1 and later use this: available credit = limit − unpaid Xero invoices − open account orders not yet invoiced in Xero. Orders aren't counted twice, and web orders paid online use no credit. Shop logins are refreshed on `xero.balances_updated`.

Without the shop plugin, nothing here runs. A Sales Order only counts as a web order if it carries `metadata['woocommerce']`.

## In the InvenTree UI

- **Xero panel** on every purchase order, sales order, company and part page:
  - status, number and link;
  - amounts due and paid;
  - Xero-only lines;
  - the bill;
  - balances;
  - recent activity.

  Buttons:
  - *Send / Update Xero now*
  - *Create / Update bill now*
  - *Refresh from Xero*
  - *Open in Xero*
  - *Unlink*
  - *Push details*
  - *Create Xero item*
- **Xero Orders dashboard widget**: search orders by reference, supplier or customer, their reference, description, Xero number, bill number or web order number. Pick *Web shop quotes* to see quote requests with their Xero quote, status, errors and *Send to Xero* / *Refresh from Xero* buttons.
  - Filter by in Xero / not in Xero / sync errors, by Xero status, or web orders only.
  - Also has *Sync now*.
- **InvenTree's own search** finds orders by Xero number too: `[Xero INV-0042]` is appended to the description (*Tag description*).
- **Plugin settings page**: connection, redirect URI, connect / test / sync / disconnect, and the activity log.
- **Django admin → Xero sync log**: full searchable history.

## Settings reference

| Group | Setting | Default |
|---|---|---|
| Safety | Write mode, let InvenTree approve, allow delete/void, import new Xero documents, contact matching, require tax types, check totals, allow unpriced lines, alert emails, audit retention | dry run, off, off, off, strict, on, on, off, superusers, 365 days |
| Connection | Connection type, Client ID/secret, scopes, public URL, tenant | OAuth |
| Schedule | Scheduled sync / interval / push on status change | on / 15 min / on |
| Purchase orders | Direction, statuses to export, placed status, use InvenTree number, statuses to import | two-way, placed+on hold+complete, Approved, on, Submitted+Approved |
| Bills | Create bills, status when complete, due days, mark PO billed, link bills entered in Xero | off, Draft, 30, on, on |
| Sales | Direction, invoice or quote, statuses to export, approve when, due days, use InvenTree number, statuses to import, issue on import | two-way, invoice, in progress+shipped+complete, never, 30, off, Approved+Paid, on |
| What InvenTree holds | Non-part lines, skip documents without parts, update lines from Xero | keep in Xero, on, on |
| Contacts | Link / create / create + update, create InvenTree companies, import balances, balance interval, balance parameter | create, on, on, 60 min, `Xero Outstanding` |
| Items | Item code from, create Xero items, create supplier parts on import | part name, off, off |
| Web shop | Integration, document for web orders, approve paid invoices, payment account | on, invoice, on, blank |
| Shop quotes | Quote requests (draft / only once sent / off), Xero edits go to the shop, re-send revised quotes automatically, part for non-part lines, default decline reason | draft, on, off, blank, blank |
| Accounting | Purchase/sales account and tax type, prices are, send currency | 300 / 200, account default, tax exclusive, off |
| Housekeeping | First import look-back, export look-back, set order link, tag description, log retention | 30, 180, on, on, 60 days |

## For other plugins

Links live in each object's metadata. They are read-only for other plugins; the Xero plugin writes them.

```python
order.metadata['xero']          # id, number, status, url, total, amount_due, amount_paid, line_ids, xero_only, bill{...}
company.metadata['xero']        # contact_id, url, balance{receivable_outstanding, receivable_overdue,
                                #                          payable_outstanding, payable_overdue, updated}
part.metadata['xero']           # item_id, item_code, url
```

Read by this plugin, written by others - bill a purchase order per supplier invoice:

```python
order.metadata['supplier_invoices'] = [   # present (even empty) = bill per invoice; each entry = one bill
    {'number': '93170001', 'date': '2026-10-24', 'due_date': '2026-11-23',
     'lines': [{'line': <PurchaseOrderLineItem pk>, 'quantity': '40'}],
     'extra': [{'description': 'Freight', 'amount': '12.50'}]},
]
order.metadata['xero']['bills']   # {invoice number: link (id, number, status, url, total, amount_due, ...)}
```

Events fired (needs *Event integration*):

| Event | Arguments |
|---|---|
| `xero.balances_updated` | `company_ids` — companies whose Xero balance changed |

Events consumed:
- InvenTree's order events.
- `purchaseorderitem.received`.
- `woocommerce.salesorder_created`, `woocommerce.quote_received`, `woocommerce.quote_accepted`.

From the shop plugin it reads the `QuoteRequest` / `QuoteItem` / `QuoteLine` models and calls its `quotes.apply_action()` (`set_lines`, `send`, `reopen`, `decline`), so a quote changed through Xero gets exactly the same checks and customer emails as one changed in the shop widget. Quote links are kept in this plugin's `XeroQuoteLink` table; the shop plugin needs no changes.

## API

All endpoints are under `/plugin/xero/api/`, with token or session auth. Anything that changes something (POST) is staff-only.

| Method | Path | |
|---|---|---|
| GET | `status/` | connection, last sync, recent activity |
| POST | `sync/` `{"direction": "all" \| "export" \| "import"}` | queue a sync (staff) |
| GET | `search/?q=&type=all\|po\|so&linked=any\|yes\|no\|error&xero_status=&source=shop` | order search |
| GET | `order/<po\|so>/<pk>/` | Xero link, bill, web order, activity |
| POST | `order/<po\|so>/<pk>/<push\|pull\|bill\|unlink\|unlink-bill>/` | per-order actions |
| GET / POST | `company/<pk>/` `{"action": "link"\|"link_id"\|"create"\|"push"\|"balance"\|"unlink"}` | contact link (`link_id` with `contact_id` links by hand) |
| GET / POST | `part/<pk>/` `{"action": "link"\|"create"\|"unlink"}` | item link |
| GET | `quotes/?q=&status=open\|sent\|closed\|error` | web shop quote requests and their Xero quotes |
| POST | `quotes/<quote request id>/<push\|pull>/` | send one quote to Xero now / apply its Xero changes now |
| GET | `order/<po\|so>/<pk>/preview/` · `preview-bill/` | what would be sent, and what would hold it (writes nothing) |
| POST | `mode/` `{"mode": "dry_run"\|"live"\|"paused", "confirm": true}` | write mode / kill switch (staff; going live needs `confirm`) |
| GET | `audit/?mode=&ref=&limit=` · `audit/?id=` | every write to Xero, with request and response (staff) |
| GET | `company/<pk>/candidates/?q=` | Xero contacts that might be this company (for linking by hand) |
| GET / POST | `check/` | last / run the read-only configuration check (staff) |
| GET / POST | `selftest/` `{"confirm": "DEMO"}` | last report / queue the live self-test, Demo Company only (staff) |
| GET | `log/?level=&doc_type=&q=&limit=` | sync log |

## Notes

- Xero limits each organisation to 60 calls a minute and 5,000 a day. The client backs off on HTTP 429.
- The OAuth refresh token expires after 60 days unused. The scheduled sync keeps it alive.
- Tokens are stored encrypted in the plugin's own table and hidden in Django admin. If InvenTree's `SECRET_KEY` changes, reconnect to Xero.
- "Open in Xero" links use Xero's organisation deep-link format.

## Development

Releases are made with `build-and-bump.ps1` (Windows PowerShell or PowerShell 7). It regenerates the settings doc, runs lint and the unit tests, bumps `inventree_xero/version.py`, stamps the changelog, commits and tags `vX.Y.Z`. See [docs/HANDOVER.md](docs/HANDOVER.md#releasing).

```bash
pip install -e . pytest pyflakes && pytest tests/test_mapping.py tests/test_safety.py   # unit tests, no InvenTree needed
npm install jsdom && node --test tests/ui.test.mjs          # UI render tests
# end-to-end against a real InvenTree with the real inventree-woocommerce plugin installed
# (a fake Xero organisation; the shop's calls out to WooCommerce and WordPress are faked):
pip install "inventree-woocommerce @ git+https://github.com/SXSLYDA/inventree-woocommerce.git"
cd /path/to/InvenTree/src/backend/InvenTree
XERO_TESTS=/path/to/inventree-xero/tests python manage.py shell -c \
  "exec(open('/path/to/inventree-xero/tests/e2e_inventree.py').read())"
```

CI runs all three against InvenTree 1.6.0 (`.github/workflows/ci.yml`), and checks `docs/SETTINGS.md` matches the code (`python tools/gen_settings_doc.py --check`). See [docs/HANDOVER.md](docs/HANDOVER.md) for the architecture and release steps. Issues and pull requests are welcome.

## Licence

MIT. See [LICENSE](LICENSE).
