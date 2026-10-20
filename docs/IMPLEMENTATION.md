# Implementation guide

A step-by-step path from nothing installed to running live against your real Xero organisation, with a check at every stage. Plan on a few weeks of elapsed time, not a few hours: most of it is letting each stage run quietly before turning on the next.

**At any point:** the **Pause all writes** button (plugin settings panel) stops everything immediately. Nothing in this guide is irreversible until stage 5, and even then everything arrives in Xero as Draft.

---

## Stage 0 — Before you start

| Need | Why | How to check |
|---|---|---|
| InvenTree **1.6 or later** | the plugin uses 1.6 order fields and events | Admin Center → About |
| The **background worker** running | the schedule, pushes, alerts and self-test run there | Admin Center → Background tasks shows the worker as running |
| **Email** set up in InvenTree | alerts about errors and held documents | send yourself a test notification |
| A **database backup** you know how to restore | normal caution before adding any plugin | `invoke backup` (or your own backup) and a test restore |
| A Xero login with **adviser / standard** access to the organisation | to create the app and approve the connection | — |
| (Optional) the **inventree-woocommerce** plugin 0.6.0+ | web orders, quote requests and credit checks | Admin Center → Plugins |

Write down InvenTree's `SECRET_KEY` location (in `config.yaml` or `INVENTREE_SECRET_KEY`). Xero tokens are encrypted with a key derived from it. If it ever changes, you just reconnect to Xero.

## Stage 1 — Create the Xero app (15 minutes)

1. Go to **developer.xero.com → My Apps → New app**.
2. Choose **Web app** (free). This guide uses a Web app.
   - The alternative, a **Custom Connection**, needs no browser sign-in and is free with the Demo Company.
   - For a real organisation it's a paid add-on (about A$10 a month), available in AU, NZ, UK and US organisations.
3. Fill in:
   - **App name:** e.g. *InvenTree*.
   - **Company or application URL:** your InvenTree URL.
   - **Redirect URI:** `https://<your-inventree>/plugin/xero/oauth/callback/`. The exact value is shown on the plugin settings panel once installed, so you can come back and correct it.
4. Create the app. Copy the **Client ID** and generate a **Client secret**, and store both in your password manager.

You don't choose permissions in the Xero app. The plugin asks for only what the features you switch on need, and the settings panel shows the list.

## Stage 2 — Install the plugin (30 minutes)

**Package installer (Ubuntu/Debian):** install the update script once, then let it install the plugin:

```bash
sudo curl -fsSL -o /usr/local/bin/inventree-xero-update \
     https://raw.githubusercontent.com/SXSLYDA/inventree-xero/main/tools/server-update.sh
sudo chmod +x /usr/local/bin/inventree-xero-update
sudo inventree-xero-update --no-check     # newest release (the check needs the plugin activated - stage 4)
```

The script:
- finds `plugins.txt` from `/etc/inventree/config.yaml`;
- adds `inventree-xero @ git+https://github.com/SXSLYDA/inventree-xero.git@vX.Y.Z`, **pinned to the release tag**, so an update never arrives by surprise;
- backs up the database;
- runs `invoke update` (install, migrations, static files);
- restarts InvenTree and confirms the version that installed.

If you use the shop plugin, add its line to `plugins.txt` by hand first, e.g. `inventree-woocommerce @ git+https://github.com/SXSLYDA/inventree-woocommerce.git@v0.6.0`.

**Docker:** add `inventree-xero @ git+https://github.com/SXSLYDA/inventree-xero.git@vX.Y.Z` (the newest release tag on GitHub) to `plugins.txt` in your data volume, run `docker compose run --rm inventree-server invoke update`, then restart the stack.

Then, in InvenTree:

1. **Admin Center → Plugin settings**: turn on **URL integration**, **Schedule integration**, **Event integration**, **App integration** and **Interface integration**.
2. Restart InvenTree again (app integration needs it). Then run `sudo inventree run invoke migrate` to create the plugin's tables: connection, sync log, quote links, write audit.
3. **Admin Center → Plugins**: activate **Xero**.

✅ **Check:** the plugin settings page shows a **Xero** panel with "Not connected" and a redirect URI. If the panel says the plugin's URLs or tables aren't loaded, restart once more and re-run `invoke migrate`.

## Stage 3 — Configure (30 minutes)

Open the plugin's settings. Leave **Safety** settings at their defaults. **Write mode stays on Dry run** until stage 5.

| Setting | What to put |
|---|---|
| Client ID / Client secret | from stage 1 |
| InvenTree public URL | e.g. `https://inventree.example.com` (must match the redirect URI host) |
| Purchase / sales account code | your Xero codes. The configuration check confirms they exist. |
| Purchase / sales tax type | e.g. **INPUT** / **OUTPUT** for Australian GST. Documents are held until these are set. |
| Prices are | *Tax exclusive* if your InvenTree prices are ex-GST (usual) |
| Alert emails | who should hear about errors and held documents |
| Purchase orders / Sales orders | *Two-way* (status comes back from Xero; new Xero documents are not imported unless you allow it) |
| Create bills | *Off* for now. You'll turn it on in stage 6. |
| Web shop quote requests | *Price and send in Xero* if you price quotes in Xero (customers get Xero's PDF; labour and freight lines go through); otherwise one of the *Priced in InvenTree* options |

Everything else can stay at its default. The full list is in [SETTINGS.md](SETTINGS.md).

## Stage 4 — Prove it against Xero's Demo Company (1 hour)

The Demo Company is a free, throwaway Xero organisation that resets every 28 days.

1. In Xero, open your name (top right) → **Demo Company**, so it exists for your login.
2. On the plugin settings panel, click **Connect to Xero**, sign in, and **choose the Demo Company** on Xero's consent screen.
3. Click **Check configuration**. Fix every **FAIL**. A WARN is advice; read it.
4. Click **Run live self-test (Demo Company only)…** and type `DEMO`.
   - It writes tagged test documents covering every flow the plugin uses: purchase orders, bills, invoices, quotes, contacts, items, discounts, tax types, line preservation, idempotency and status rules.
   - It refuses to run on a real organisation.
5. After about a minute, refresh: the report appears under **Last live self-test**.
   - **All PASS:** carry on.
   - **Any FAIL or WARN:** click *Copy as text* and send the report to whoever maintains your plugin (or open a GitHub issue). These are exactly the places where Xero behaves differently to what the plugin expects.
6. Open the **"Open in Xero" links** listed in the report and check each one lands on the right document.
7. Still in the Demo Company:
   - switch **Write mode** to **Live**;
   - create a purchase order in InvenTree for a supplier, place it, and click **Update Xero now** on its Xero panel;
   - check it appears in Xero as a Draft;
   - try a sales order too;
   - then switch back to **Dry run**.
8. **Disconnect** the Demo Company.

From the command line, the same two checks are:

```bash
sudo inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py xero_check
sudo inventree run python3 /opt/inventree/src/backend/InvenTree/manage.py xero_selftest --confirm-demo
```

A sample self-test report is at the end of this guide.

## Stage 5 — Connect your real organisation in Dry run (1–2 weeks)

1. **Connect to Xero** again and choose your **real organisation**. Write mode is still **Dry run**: nothing is written.
2. **Check configuration** again. It reads only, so it's safe on the real organisation. Fix every FAIL. Note the **lock date**: Xero rejects documents dated on or before it.
3. Let it run for one to two weeks of normal work. Each day, glance at:
   - **Dashboard → Xero Orders**, filter **Held for review**. Each held order says why (no price, no tax type, a contact that needs linking by hand, totals that don't match). Fix the cause in InvenTree, or link the contact on the company's Xero panel. Holds clear themselves on the next sync.
   - Any order's **Preview** button: the exact lines, prices, accounts and tax types that would go to Xero.
   - The **sync log** (bottom of the settings panel, or Django admin → *Xero sync log*): every *"Dry run - would create purchase order: 3 line(s), 412.50 Exclusive (DRAFT)"*.
4. Compare a handful of previews with what you'd have keyed into Xero by hand.

**Contacts are the most common snag.** If Xero has "Acme Cables Pty Ltd" and InvenTree has "Acme Cables", the plugin won't guess: it holds the order. Open the company in InvenTree, use **Find in Xero** on its Xero panel, and click **Link** on the right contact. Do this once per company.

## Stage 6 — Go live in phases (a week or more each)

Switch **Write mode** to **Live** on the settings panel; it asks you to confirm. Then turn features on one at a time:

| Phase | Turn on | Check in Xero |
|---|---|---|
| A | *Purchase orders* (already on) | Placed POs appear as **Draft** POs. Approve them in Xero as you normally would; that's what emails the supplier. |
| B | *Create bills*: **As stock is received** | Draft bills appear and grow as you receive stock. Add the supplier's invoice number and freight in Xero, then approve. Those Xero lines are kept. |
| C | *Sales orders* (already on) | Issued sales orders appear as **Draft** invoices. Approve in Xero when you invoice. |
| D | Web shop orders and quote requests | Web orders become draft invoices. Quote requests appear as draft quotes; marking one Sent in Xero sends it to the customer with Xero's PDF. Check one quote with labour/freight and a discount end to end, and compare the shop's prices with Xero's. |

After each phase, run **Check configuration** and look at the held list and the **Recent writes to Xero** table on the settings panel.

**Leave these off unless you really want them:**
- *Let InvenTree approve documents*
- *Allow delete / void*
- *Import new Xero documents*
- recording web shop payments: let your Stripe or bank feed do it

## Day to day

- **Held documents** are emailed to *Alert emails*, once a day per problem. They also show on each order's Xero panel and under *Held for review* in the dashboard widget.
- **Approved in Xero = Xero wins.** Once you approve a document in Xero, later InvenTree changes to it are held, never written over it. Change it in Xero, or set it back to Draft in Xero and the change will follow.
- **Cancelling** an order in InvenTree never deletes anything in Xero. You get a held notice to void or delete it in Xero yourself.
- **Every write** is recorded with what was sent and Xero's reply: Django admin → *Xero writes (audit)*.

## Updating the plugin later

On the server:

```bash
sudo inventree-xero-update --check-only    # what's installed, pinned and newest - changes nothing
sudo inventree-xero-update                 # newest release (or name one: sudo inventree-xero-update v1.4.2)
```

It backs up the database first and runs the configuration check afterwards. If the update fails, it puts `plugins.txt` back and tells you how to reinstall the previous version. Read the release's notes in `CHANGELOG.md` before updating; anything that changes behaviour is listed there.

**Rolling back:** `sudo inventree-xero-update v1.4.1 --allow-downgrade`. A newer version's database changes are not undone. Only roll back between versions with the same migrations (the changelog says when a release adds one), or restore the backup the script made.

## If something goes wrong

1. Click **Pause all writes** on the settings panel. Nothing is sent or changed from that moment: no schedule, no events, no buttons.
2. Look at **Recent writes to Xero** and the sync log to see what was sent.
3. Fix it in Xero by hand. The audit has the exact request and response for each write.
4. Switch to **Dry run** and preview until you're happy, then back to **Live**.

To stop using the plugin entirely: pause writes, disconnect (this revokes the token in Xero), then deactivate the plugin. Links stored on orders stay as harmless metadata.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Connect to Xero" → Xero error *invalid redirect_uri* | The redirect URI in the Xero app must match the settings panel exactly, including `https://` and the trailing `/` |
| Xero error *invalid scope for client* | Your Xero app predates granular scopes, or a scope isn't available to it. Remove the plugin's custom *OAuth scopes* setting (leave it blank) and reconnect |
| "Xero refresh token is no longer valid" | Not used for 60 days, or revoked in Xero. Reconnect from the settings panel |
| "stored Xero tokens could not be decrypted" | InvenTree's `SECRET_KEY` changed. Reconnect |
| Settings panel lists **missing permissions** | You switched on a feature that needs more. Reconnect to grant it |
| Nothing syncs | Write mode *Paused*? Background worker running? *Scheduled sync* on? Check configuration says which |
| Order **held**: "tax type is not set" | Set *Purchase/Sales tax type* (AU: INPUT / OUTPUT) |
| Order **held**: "lines add up to X but the InvenTree order total is Y" | A price or discount changed mid-edit, or a currency conversion is missing. Re-save the order; check its currency |
| Order **held**: contact | Company panel → **Find in Xero** → **Link** |
| Order **held**: "approved in Xero" | Working as designed. Change it in Xero, or set it back to Draft there |
| Xero error mentioning the **lock date** | The order date is in a locked period. Change the date or ask your accountant |
| Rate limit messages | Xero allows 60 calls a minute and 5,000 a day per organisation. The plugin waits and retries; lower the sync frequency if it's persistent |

---

## Sample self-test report

This is what a clean run looks like (generated against the plugin's test Xero; a real Demo Company will show different numbers and IDs):

```
inventree-xero LIVE self-test (Xero Demo Company)
28 passed, 0 warnings, 0 failed

[PASS] Organisation: "Demo Company (AU)" is a Demo Company (AU, AUD) - test documents are tagged INVENTREE-XERO-SELFTEST-...
[PASS] Chart of accounts: Using sales 200, purchases 300, tax OUTPUT / INPUT
[PASS] Contact: create: created "InvenTree self-test ..." with email, phone and ABN
[PASS] Contact: search by name: found by searchTerm; list rows include EmailAddress, TaxNumber for strict matching
[PASS] Contact: update details: detail update saved
[INFO] Contact: balances: list by IDs does NOT include Balances; single contact does NOT include Balances (the plugin handles both)
[PASS] Items: create and list: item IXT... created and listed
[PASS] Purchase order: create Draft: IXT-PO-... created as Draft; discount kept; subtotal 27.50 as InvenTree adds it up
[PASS] Idempotency key: no duplicates on retry: repeating the same request returned the same purchase order (no duplicate)
[PASS] Purchase order: keep Xero-only lines: line set replaced; the Xero-only freight line survived the update
[PASS] Incremental import (If-Modified-Since): If-Modified-Since returned it (8 changed in 15 min)
[PASS] Purchase order: approve: now AUTHORISED
[PASS] Purchase order: cannot go back to Draft: Xero refused it, as the plugin expects (...)
[PASS] Purchase order: mark Billed: now BILLED
[PASS] Purchase order: delete a draft: a draft purchase order can be deleted
[PASS] Invoice: create Draft: INV-... created as Draft; 5% discount; tax OUTPUT
[PASS] Invoice: approve: now AUTHORISED
[INFO] Invoice: record payment: Skipped - the connection has no payments permission (...)
[PASS] Invoice: void: an unpaid approved invoice can be voided
[PASS] Invoice: delete a draft: a draft invoice can be deleted
[PASS] Bill: create Draft: draft bill created with the PO number in its reference
[PASS] Bill: follow receipts, keep Xero-only lines: received quantity updated; supplier freight added in Xero kept
[PASS] Bill: approve: now AUTHORISED
[PASS] Quote: create Draft: QU-... created as Draft with reference, title and expiry
[PASS] Quote: mark Sent: now SENT
[PASS] Quote: PDF (what the shop emails customers): Xero printed it (.. KB) - the shop sends customers this PDF
[PASS] Quote: Sent back to Draft (revision): now DRAFT - revised quotes can go back to Draft, as the plugin expects
[PASS] Quote: Sent again: now SENT
[PASS] Quote: accept: now ACCEPTED
[PASS] Quote: mark Invoiced: now INVOICED
[PASS] Quote: decline with a reason: a sent quote can be declined; its Summary (the reason the shop sends) is kept
[INFO] "Open in Xero" links: Open these while signed in to the Demo Company and check each lands on the right page: ...
[INFO] Clean-up: test contact archived; test item deleted; ...
```

**What a FAIL means:**

| Line fails | What it means for you |
|---|---|
| Idempotency key | Retries after a dropped connection could duplicate documents. Report it; don't go live until it's fixed |
| Keep Xero-only lines | Lines you add in Xero (freight, labour) could be lost when InvenTree updates a document. Report it |
| Sent back to Draft (revision) | Shows as a WARN with advice. Either turn on *Re-send revised quotes automatically*, or revise sent quotes in the shop widget |
| Cannot go back to Draft | Shows as a WARN if Xero allowed it. Harmless: the plugin never tries to do it |
| Anything with account / tax | Check the codes in the settings exist and are active in that organisation |
| Contact: search by name | Contact matching would not find existing contacts. Report it |
