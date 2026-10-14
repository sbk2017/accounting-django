# Local Accounting — Django edition

This rebuild uses Django's authentication, admin, migrations, and structured logs. It is a reviewable first version for a trusted local network, with one company and AED as base currency.

## Start with Docker

1. Copy `.env.example` to `.env`.
2. Set `DJANGO_SECRET_KEY` to a long random value, `ALLOWED_HOSTS` to the local server's IP or hostname, and `APP_PORT` to the desired port (for example, `8090`). The same `APP_PORT` controls both the container listener and host mapping in `compose.yaml`.
3. Run `docker compose up -d --build`.
4. Create a login account: `docker compose exec accounting python manage.py createsuperuser`. Enter a username and password when prompted.
5. Open `http://YOUR-SERVER-IP:APP_PORT/accounts/login/`. Use that same server address after logging in.

The first run applies migrations and seeds example accounts. The database and logs are in `./data`. Stop the container before making a simple file copy backup, or use SQLite's online backup API.

## Debugging sign-in

- `docker compose logs -f accounting` shows startup errors and requests.
- `./data/app.log` contains timestamped exceptions from imports and accounting actions.
- If login succeeds but the next page returns `DisallowedHost`, put the exact server IP or hostname in `ALLOWED_HOSTS` and restart.
- If Django reports a CSRF origin error behind a proxy, set `CSRF_TRUSTED_ORIGINS` to its full origin, e.g. `http://192.168.1.10:8090`, then restart.
- If the browser returns to the login page, check that cookies are enabled and that you always use the same hostname/IP to sign in and navigate. Django stores login sessions in SQLite.
- A failed password shows an error on the sign-in page. Users can be added or reset under `/admin/` by a superuser.

## Features and scope

### Chart of accounts CSV backup

On **Chart of accounts**, use **Download chart of accounts CSV** before resetting the database. After rebuilding and signing in as staff, upload the file using **Upload accounts CSV** on the same page.

CSV columns are `code,name,type,parent_code,active`. Downloads preserve parent relationships and inactive accounts. Imports match by account code, update existing accounts (including seeded accounts), and add missing ones without deleting other accounts. Parents may appear anywhere in the file. Invalid rows roll back the whole import; changing the type of an account already used in a journal remains blocked. Preserve account codes as text in spreadsheet editors to retain leading zeros.

The CSV backs up the chart only, not transactions, users, or settings. No database migration is needed; rebuild the Docker app to install this feature.

### Journal screen update

The journal list displays total debit amount in AED, filters for every displayed column (including date and amount ranges), and 20 vouchers per page. Staff can select draft vouchers on the current page and post them together. A locked, missing, or already posted voucher rejects the entire batch, so no partial posting occurs. Filters are retained after posting and during pagination.

Manual vouchers now use account dropdowns and separate debit/credit inputs, with add/remove line controls and live totals. Server validation rejects unbalanced entries and retains submitted values so errors can be corrected.

To update an existing Django installation, replace the application source files, keep your `.env` and `data` folder, then run `docker compose up -d --build`. This update does not change the database schema.

- Account chart, balanced JV drafts, posting, reversals, locked months, audit events.
- Recurring monthly, quarterly, or annual JV templates with start and optional end date. **Generate due drafts** is manual, idempotent, and skips locked months.
- Expense/COGS CSV import creates drafts. Add the optional `supplier_trn` column (`trn` is also accepted) to retain the supplier TRN as text alongside the supplier name, visible on the journal detail page for future VAT reporting. Existing files without a TRN column still work; blank TRNs are allowed. Preserve TRNs as text when editing spreadsheets. The sample `ledger/expenses.csv` includes a fictional TRN. This stores the identifier but does not verify registration or generate a VAT filing. To add a TRN to an already imported entry, edit its custom fields in Django Admin; uploading a modified CSV creates new entries because duplicate detection uses the file contents. Bank CSV import stages rows for match or draft creation. Import errors roll back the whole file.
- Dolibarr validated customer invoice sync includes unpaid and paid invoices across all pages and creates drafts. Draft and cancelled invoices are excluded; leave the invoice date filter empty to include all dates. Previously imported invoices are skipped. Configure `DOLIBARR_URL` (base API URL ending in `/api/index.php`) and `DOLIBARR_API_KEY` in `.env`. It has not yet been tested against your Dolibarr installation. Payments are not synchronized.
- Monthly/annual P&L, sales, expenses, VAT management summary, trial balance, general ledger, and cumulative balance sheet.
- Django Admin manages users and provides model inspection. Staff users can post/reverse entries, manage accounts and settings; signed-in users can enter draft JVs and import data.

Accounting figures are not a filed VAT return. Verify tax treatment and account mappings with your accountant before production use. Formal bank reconciliation statement, PDF export, and user-defined custom-field forms are next-stage work. `custom_fields` JSON is present in the model so added fields can be migrated cleanly.

**Database compatibility:** this Django version does not automatically read the previous standalone app's SQLite schema. If you entered real data there, keep that `data` folder and migrate it explicitly before switching; do not point this Django app at the old database.

## Local development

Install `requirements.txt`, set `DJANGO_SECRET_KEY`, then run `python manage.py migrate`, `python manage.py bootstrap_accounts`, `python manage.py createsuperuser`, and `python manage.py runserver 0.0.0.0:8080`. The `.env` file is read by Docker Compose; outside Docker, export variables in your shell.

### Bank statement import

In **Imports → Bank CSV**, upload columns `date,Description,ref,debit,credit` and click **Next: review statement**. Select one bank account at the top and an opposite account for every imported line, then **Save statement rows**. The Bank review page lets you match posted entries or create drafts. Statement debits are withdrawals (credit the ledger bank account); statement credits are receipts (debit the ledger bank account).

Dates accept `YYYY-MM-DD`, `DD/MM/YYYY`, or `DD-MM-YYYY`. Headers ignore case and surrounding spaces; `Value Date`, `Ref/Cheque No`, `Debit Amount`, and `Credit Amount` are also supported. Each row must have exactly one positive amount, with the other side blank or zero. Reimporting the identical file for the same bank skips existing rows. Modified files are treated as new imports. Uploading another statement replaces your pending review. See `ledger/bank.csv` for an example.

### Reporting updates and AI API

Trial balance rows now show the period balance (debit minus credit). General
ledger rows include description, journal source, and source identifier.

The read-only `/api/v1/accounts/`, `/api/v1/reports/`, and
`/api/v1/ledger/{account_code}/` endpoints support AI reporting with a dedicated
`REPORTING_API_TOKEN`. Access is disabled until that environment variable is set.
See [AI reporting API documentation](docs/ai-reporting-api.md) for setup,
authentication, filters, pagination, amount conventions, and examples.
Rebuild/restart the application to apply these changes; no migration is needed.
