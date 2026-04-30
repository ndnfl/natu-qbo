# natu-qbo

Apply CSV-driven changes to QuickBooks Online transactions.

Workflow: export a Transaction Detail report from QBO, edit a copy of the CSV
(or build one from scratch) with the changes you want, then run the applier.

## v1 scope

Edit existing posted transactions:
- Account reclassification (line item)
- Class / Location
- Customer / Vendor (entity ref)
- Memo / Description

Out of scope (v2): mapping pending bank-feed "For Review" items.

## One-time setup

### 1. Host the OAuth redirect bouncer (one-time per user / fork)

Intuit's Production OAuth keys require an HTTPS redirect URI. They reject
`http://localhost...` for production. To avoid making every user run a tunnel
(ngrok etc.), this repo ships a static bouncer page at [`docs/index.html`](docs/index.html)
that you publish on GitHub Pages.

1. Push this repo to your own GitHub account (e.g. `<you>/natu-qbo`).
2. In the repo on GitHub: **Settings → Pages**.
3. Under **Source**, choose **Deploy from a branch**, select `main` and `/docs`.
4. Save. After ~30 seconds, your bouncer URL is `https://<you>.github.io/natu-qbo/`.

The page is a stateless redirect — it just reads `?code=...&state=...&realmId=...`
from the URL and forwards them to `http://localhost:<port>/callback` on your
machine. The port is encoded in the OAuth `state` param so multiple machines
can share the same bouncer URL.

### 2. Create an Intuit Developer app

1. Sign in at https://developer.intuit.com with your Intuit ID.
2. Dashboard → **Create an app** → choose **QuickBooks Online and Payments**.
3. Name it `natu-qbo` (or whatever). Scope: `com.intuit.quickbooks.accounting`.
4. Under **Keys & credentials**, switch to the **Production** tab.
5. Add redirect URI: `https://<you>.github.io/natu-qbo/` (from step 1, with trailing slash).
6. Copy the **Client ID** and **Client Secret** into `.env`.
7. Set `QBO_REDIRECT_URI=https://<you>.github.io/natu-qbo/` in `.env`.

> **Sandbox shortcut:** If you just want to try the flow against QBO's sandbox
> first, switch to the **Development** tab in step 4, register
> `http://localhost:8765/callback` as the redirect URI, set `QBO_ENV=sandbox`
> in `.env`, and skip step 1 entirely.

Note: Production keys for a self-distributed app you only connect to your own
QBO company do not require Intuit's app review.

### 3. Install + configure

```bash
cd natu-qbo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in QBO_CLIENT_ID, QBO_CLIENT_SECRET, and QBO_REDIRECT_URI
```

### 4. Authorize against your QBO company

```bash
python -m src.auth
```

This opens a browser, you log into QBO and pick the company, the script writes
`tokens.json`. Refresh tokens last 100 days; access tokens auto-refresh.

## Applying changes

```bash
python -m src.apply path/to/changes.csv --dry-run
python -m src.apply path/to/changes.csv
```

CSV columns: `txn_type, txn_id, line_id, field, new_value`

- `txn_type`: QBO entity name — `JournalEntry`, `Bill`, `Invoice`, `Purchase` (= cash/check/CC expense), `Deposit`
- `txn_id`: QBO internal Id (not the displayed Doc Number — see below)
- `line_id`: required for line-level edits (account, class on a line, line memo). Empty for header-level edits (vendor on a Bill, location on most txns, txn-level memo)
- `field`: `account` | `class` | `location` | `customer` | `vendor` | `memo`
- `new_value`: name of the target Account/Class/Department/Customer/Vendor, or memo text

See `changes_example.csv`.

### Finding the QBO txn Id

The displayed reference (e.g. `JE-1042`) is not the API Id. Use the helper:

```bash
python -m src.find_id JournalEntry --doc-number JE-1042
python -m src.find_id Bill --vendor "Amazon Web Services" --date 2026-04-15
python -m src.find_id Invoice --customer "Acme Corp" --date-range 2026-04-01 2026-04-30
python -m src.find_id Purchase --amount 1234.56
```

Or open the transaction in QBO — the URL contains `txnId=<id>`.

### Notes / known limits

- Item-based lines (Invoice line items, item-based Bill lines) get their account from the Item — change the Item, not the line account.
- All edits to one transaction are batched into a single sparse update (all-or-nothing per txn).
- `tokens.json` lives in this directory and is gitignored; refresh tokens last 100 days.

