# bexio login

The bexio scripts log in through Benchi's bexio OAuth app, with read-only scopes.
bexio enforces the scopes, so the token cannot change the account, whatever the code does.

## Log in once

The Mac mini is headless, so the login runs from the MacBook through an ssh tunnel. The
browser on the MacBook returns to `http://localhost:8765/callback`; the tunnel hands that
redirect to the listener on 127.0.0.1:8765 on the mini.

1. On the MacBook, open the tunnel and keep it open until the login is done. Port 8765
   must be free on the MacBook:

   ```sh
   ssh -L 8765:127.0.0.1:8765 <user>@<mac-mini>
   ```

2. In that ssh session, run the login from the depot checkout on the mini:

   ```sh
   finance/bexio/oauth.py login
   ```

   The command is an av launcher (its first line injects the client id and secret from the
   Automic Vault). It prints the bexio authorize URL. `webbrowser.open()` does nothing on the
   headless mini, which is expected: the URL is printed for the MacBook.

3. Open the printed URL in the MacBook's browser and approve the login at bexio.
4. The terminal says that the refresh token was saved as `BEXIO_REFRESH_TOKEN` in the
   Automic Vault. The token itself is never printed. Then close the ssh session.

The save (`av save`) waits for Benchi's approval in the vault app. If the vault app asks on
the mini's screen, use Screen Sharing for that one step.

## Log in from the MacBook (tailnet login page)

Instead of the ssh tunnel, the login can be started from the MacBook
through a page on the tailnet. `finance/bexio/login_helper.py` serves it on
127.0.0.1:8814, as an av launcher, so the client id and secret come from the Automic Vault
and never from a file. `tailscale serve` puts it on the tailnet over https. Never Funnel:
the page starts logins and must not be public.

- `/login` redirects the browser to bexio with the read-only scopes. Each login has a
  one-time state, valid 300 seconds. A new `/login` replaces a waiting one.
- `/callback` exchanges the code and saves the new refresh token in the same vault value
  as `oauth.py login`. The page shows `logged in, scopes: ...`, never a token.
- Only Benchi's tailnet login gets in: every request must carry the `Tailscale-User-Login`
  header that `tailscale serve` adds, and it must equal `--tailnet-user`. Anything else is 403.
  Nothing is logged; the page and the terminal print only the outcome.

Once installed, this page is the alternative to the tunnel login above. Its install needs
Benchi in the bexio developer portal (install step 1 below).

Install on the Mac mini (once):

1. Register the callback on the bexio OAuth app, exactly as below. It is added in the
   bexio developer portal next to `http://localhost:8765/callback`, which stays as the fallback.
   The URL is https, not http:
   `https://<mac-mini>.<tailnet>.ts.net:<https-port>/callback`
2. Copy `finance/bexio/ch.bi-concepts.bexio-login.plist` to `~/Library/LaunchAgents/`, with
   `REDIRECT_URI_PLACEHOLDER` replaced by that URL and `TAILNET_USER_PLACEHOLDER` by Benchi's
   tailnet login, then load it in Benchi's session:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ch.bi-concepts.bexio-login.plist`
3. Expose it on the tailnet only, with the Homebrew tailscale (the `~/.local/bin/tailscale`
   wrapper points at a Tailscale.app that is not installed):
   `/opt/homebrew/bin/tailscale serve --bg --https=<https-port> http://127.0.0.1:8814`

Then open `https://<mac-mini>.<tailnet>.ts.net:<https-port>/login` on the MacBook
and approve the login at bexio. The page must say `logged in`.

The tunnel login above (`oauth.py login`) stays the fallback.

## What the scopes allow

Every scope of the read-only login is read-only: `openid offline_access` for the login and the refresh,
and `*_show` for contacts, notes, articles, invoices, offers, orders, deliveries,
bills, expenses, bank accounts and bank payments. There is no
`accounting` scope and no `*_edit` scope. The list is the `SCOPE` constant in
`oauth.py`.

## Which login reads what

Reads take `BEXIO_READ_TOKEN`, the read-scoped personal access token, first: `client.py`
takes it for every GET where it is set, and a personal access token does not rotate
anything, so a read never spends or re-saves the stored OAuth refresh token. Only where no
read token is set does `client.py` run the read-only OAuth login, whose refresh token
(`BEXIO_REFRESH_TOKEN`) the launchers inject from the Automic Vault. bexio enforces the
scopes, so neither token can change the account.

The export reads with the same one token. Live check of 2026-10-14 (yardmaster ran
`inventory.py` with only `BEXIO_READ_TOKEN`, against the OAuth report of the same day):
the read token reaches every entity the OAuth login did, plus the ones the OAuth login
had refused with "no access (token scope)" — manual entries, journal lines, banking
transactions and files/attachments — because bexio grants their `accounting` and `file`
scopes only with write access. The export login of before (`oauth.py login
--export-scope`, its own vault value `BEXIO_EXPORT_REFRESH_TOKEN`) is therefore gone;
see "Why there is no export login" below. The accounting/file run needs no login step:

```sh
finance/bexio/export.py --out /Users/bsaladin/ws_yardr_finance/private/bexio-export/<date>-full --only manual_entries --only journal --only bank_transactions --only files
```

## av proxy and the read-only path

IT asked whether `av proxy +BEXIO_TOKEN` (Automic Vault) could be the read-only path. It cannot:
what `av proxy` authorizes is a Proxy Session, one canonical origin and exact Secret Names, and
the HTTP method plays no part in it (no method flag in `av proxy --help`, session approval bound
to "the exact canonical origin and exact Secret Names", and Automic Vault's own ADR 0017:
exact-origin rules "cannot prevent misuse of an approved service"). Once api.bexio.com is allowed
for a session, POST, PUT, PATCH and DELETE pass exactly like GET. The read-only path therefore
does not come from a proxy at all: it comes from the credential. The Automic Vault holds
`BEXIO_READ_TOKEN`, a read-scoped personal access token (IT proposal, Benchi approved):
`inventory.py` injects only that name with its av line — no client id, no client secret, no
refresh token ride along, so the blessed read script holds nothing write-capable — and
`client.py` takes the read token for every GET where it is set. `export.py` injects only
that name too: it reads everything with the read token (live finding of 2026-10-14, see
above). The write-capable `BEXIO_TOKEN` stays with
`upload_bill.py` and `create_contact_and_bill.py`, which POST; repair also PUTs one reviewed
individual bill endpoint.

## One run for everything (`--complete`)

`export.py --complete` exports every entity, the document PDFs (invoices, offers, orders,
deliveries, credit vouchers) and the payroll, read-only, into
`private/bexio-export/<date>-complete/`. The payroll goes under its `payroll/` folder, with
its own manifest. The top manifest has a count per entity, the refused records under
`failed`, and the document kinds that were skipped (their entity file was not written).

```sh
finance/bexio/export.py --complete
```

It reads everything with the read token, as the default run does.
A document PDF is asked raw first and, on HTTP 415, in the JSON form (base64 content). Entities
whose paths are not yet checked against the live API (credit vouchers, file usage, expense
attachments) say so in their refusals; read those in the manifest first.

## Why there is no export login

Until 2026-10-14 the export had a second OAuth login with the `accounting` and `file`
scopes (vault value `BEXIO_EXPORT_REFRESH_TOKEN`, `oauth.py login --export-scope`), meant
for manual entries, journal, bank transactions and files: bexio grants those scopes only
with write access, so the read-only OAuth login could not read them. The live check of
that day (yardmaster, `inventory.py` with only the read token, against the OAuth report
of the same day) found the read token reaches all of them anyway — plus file and bill
attachments. The export refresh token had also never been stored in the vault, so the
run died at `av inject`. The export login was therefore removed instead of set up:
`export.py` injects only `BEXIO_READ_TOKEN`, `oauth.py` and the tailnet login page have
only the read-only login, and the vault keeps one bexio value. Nothing to log in, nothing
to remove after a run.

## How the scripts use it

Each run takes the refresh token from the environment, where the launcher's av line
injected it from the Automic Vault. bexio rotates the refresh token on every refresh:
the new one is saved back to the same vault value with `av save --stdin` (Benchi approves
each save in the vault app), before the new access token is used. If that save fails the
script stops: the rotated token is then spent, so a new login is needed (`login_helper.py`
or `oauth.py login`). The access token is kept in memory only.

Reads prefer `BEXIO_READ_TOKEN`, the read-scoped personal access token the launchers
inject from the Automic Vault; it can only read, and using it never touches the vault.
Where no read token is set, the read-only OAuth login runs as before — only then is a
refresh, and with it the rotation of the stored token, possible. `client.py` never reads
`BEXIO_TOKEN`, the write-capable token of the upload scripts.

## Log in again

If the refresh token expires or is revoked, the scripts stop with
an error that names bexio's answer (for example `invalid_grant`) and says to run
`... oauth.py login` again. Run the login
command above again. A new login replaces the stored token.

Revoking the old personal access token (`BEXIO_TOKEN`) is a separate step, done in
bexio once the scripts run on this login.

## Master-data pipeline (export, then import)

1. Export, read-only, to private JSON files (needs the read token, an av launcher):

       finance/bexio/export.py

2. One-off per site, as Administrator in the backend container (the API user may not
   write these): `finance/scripts/swiss-setup.sh fields currencies`, and the banks
   that the Bank Accounts need:

       BANKS="$(python3 finance/bexio/import_master.py --print-banks)" finance/scripts/swiss-setup.sh banks

3. Import into ERPNext as `api-agent@finance.local` (token file `~/ws_yardr_finance/.erpnext-api`):

       python3 finance/bexio/import_master.py --dry-run   # totals only
       python3 finance/bexio/import_master.py

Each record is keyed by `bexio_id`; a second run changes nothing. Take a backup
first (`bench --site frontend backup`). The export directory is company data and
stays under `~/ws_yardr_finance/private/`, never in the repository.

## Sales documents (invoices, credit notes, orders, offers, deliveries)

`import_sales.py` maps the exported sales documents to ERPNext: `invoices.json`
to Sales Invoice, `credit_vouchers.json` to Sales Invoice with `is_return`
against the original, `orders.json` to Sales Order, `offers.json` to Quotation,
`deliveries.json` to Delivery Note (a draft: no stock moves, no GL).
Each record is read with its positions (the single-document call). bexio gives
a delivery a total of zero though its positions carry prices: such a Delivery
Note keeps each row's quantity at zero rate, so it agrees with bexio's total
(the prices stay in the private export). The
functions return the ERPNext document as a dict. Nothing is submitted, and no
GL posts: the documents are drafts (docstatus 0). The posting plan
(finance-3qsp) decides how they post; the submit is erp-a2ma's.

    python3 finance/bexio/import_sales.py --dry-run [--export DIR]
    python3 finance/bexio/import_sales.py --apply [--export DIR]

`--apply` writes the invoices, credit notes, orders, offers and deliveries as drafts (orders are Sales Orders,
offers Quotations and deliveries Delivery Notes, named by bexio's document number; none is ever submitted, so they
post no GL). A Delivery Note needs the `bexio_id` custom field on its doctype, which `swiss-setup.py` adds: run it
before the first `--apply` that carries deliveries. The plan goes to
`<private>/bexio-sales-drafts.json`, and the loader
(`finance/scripts/bexio-drafts.sh`, `bexio-drafts.py`) inserts it inside the
backend container as Administrator. Each draft is named by bexio's
`document_nr` (set on insert, so the ACC-SINV series does not move) and keyed by
`bexio_id`: a rerun updates a draft in place, skips a submitted one, and changes
nothing that is already right. A foreign-currency invoice is booked in CHF, as
bexio books it (see Currencies below).

`finance/scripts/bexio-drafts.sh <plan.json> submit` also submits each draft
after it is loaded, in posting date order: that is the step that writes the GL.
A plan's `keep_draft` lists the bexio ids that stay drafts; a Sales Order, Quotation or Delivery Note always stays one. A document already
submitted is skipped, so a second run submits nothing; a draft that fails
validation on submit is rolled back and listed by bexio id; a document that
was new in that run is rolled back whole (not left as a draft).

The dry run reads ERPNext and prints totals only: counts per export file, the
CHF net, tax and gross per year, and the invoice status counts (8 open, 9
paid). The differences and unmapped records, by bexio id, go to
`<private>/bexio-sales-differences.txt`.

- VAT: each position's bexio tax id is looked up in the Sales Taxes and Charges
  Templates by their `bexio_id`. A tax id without a template is unmapped; the
  rate never picks a template. The tax rows are computed from the positions,
  and bexio's own tax per rate is compared, not copied.
- Free-text positions and text lines go to the item `bexio Position`; the
  description keeps the bexio text. A position discount goes to ERPNext's
  discount fields. A bexio discount row becomes the document's discount (the
  lines less bexio's net); subtotal rows are left out. Prices that include the
  VAT are mapped with the tax row marked as included. An unknown position type
  is unmapped, not guessed.
- The total is bexio's `total` (the VAT included, after the discounts). A
  difference of up to 5 rappen goes into the last tax row; a larger one is
  unmapped and listed by bexio id.
- Where no tax row can take a difference of up to 5 rappen (prices that include
  the VAT, or a document bexio charged no VAT on), an invoice, order or offer
  takes it as bexio booked it: a positive difference as one more item line `Rundung (bexio Total)`
  on the first line's income account, without a VAT code; a negative one as the
  grand-total discount (`apply_discount_on` "Grand Total"). The tax rows stay as
  ERPNext computes them, so the tax can be a rappen off bexio's; the differences
  line names the rappen and how they were taken. Credit notes keep the refusal,
  and so does a negative difference on a document that also has a document
  discount, since ERPNext has one discount field per document.
- The free-text items `bexio Position` (sales) and `bexio Aufwand` (purchases)
  are created by `import_master.py`, not by bexio.
- Credit notes get `bexio_id` `credit-<id>`, since a credit note and an invoice
  can share an id. Their link to the invoice is the field `invoice_id`, not yet
  confirmed against a real export.
- Document number: the name of the draft, and kept in `remarks` ("bexio Nr. ...").
  The dry run lists any field the ERPNext doctype does not have.
- Rounding: ERPNext cannot carry cents in its rounding adjustment (it recomputes
  it from the grand total), so a difference of up to 5 rappen goes into the last
  tax row, or where there is none, into the `Rundung` line or the grand-total
  discount described above.
- Quantities: the free-text item (`bexio Position`) takes whole quantities, and
  ERPNext keeps a quantity to three places. A line of a fraction, of more than
  three places, or with a discount on the free-text item is one unit at its
  amount, and its quantity and discount stay in its text ("1.58 x 1000.00 less
  10%: ..."). A zero-rate row keeps a zero price list rate, so ERPNext does not
  fill the free-text item's selling price into it.
- Not written by `--apply`: an invoice, order or offer whose total differs from bexio's by more
  than 5 rappen, or by a difference no line or discount can take. It is listed by
  bexio id in `<private>/bexio-sales-differences.txt` and in the loader's output.

### Credit note (import_credit_note.py)

bexio's credit-voucher endpoint answers 404, so the export has no credit note
file. The one credit note is built from the export's two `KbCreditVoucher`
journal lines (revenue and VAT, both against the receivables), the payment row
of the invoice it is applied to (its `kb_credit_voucher_id`) and that invoice.
It becomes a Sales Invoice return against the invoice, with a negative item and
a negative VAT row, named by bexio's own number from the payment row's text, and
`update_outstanding_for_self` 0 so the return reduces the invoice's outstanding.
The module refuses a second credit note rather than guessing.

    python3 finance/bexio/import_credit_note.py [--export DIR] [--write FILE]

A dry run reads ERPNext and prints the totals; `--write` also writes the draft
plan for the loader (`sh finance/scripts/bexio-drafts.sh <plan> submit`), which
submits it. Its customer, currency and VAT account come from the same lookups as
the invoices. The address is not copied: the sales documents take their customer
only, as the invoices do, so the return's address is the customer's.

## Currencies

ERPNext 16 requires the party account's currency to equal the document's, and the
receivable and payable accounts are CHF. So a foreign-currency document is booked in
CHF, as bexio books it on 1100 and 2000: its conversion rate is 1, and each line rate
and tax is bexio's amount times the rate, to the cent. The original currency and
amount, with the rate, go into the remarks (`bexio: USD 1234.00 @ 0.90199`; a rate
from the ECB adds its day).

The rate is the one bexio booked the document at: the bill's `exchange_rate`, or for
an invoice the `currency_factor` of its receivable line in the journal. The ECB's
rate of the invoice date is used only where neither is given (and listed as a
difference).

A foreign document is checked against bexio's own CHF booking before it is written:
the CHF total of an invoice must be what the journal books on 1100 for it, and of a
bill what it books on 2000, within 5 rappen. Otherwise it is left out, with the
difference, and the journal's lines (`journal.json`, `accounts.json`) are the source.
The chart's EUR and USD accounts (1101, 2001) stay unused, as bexio did not use them.

## Attachments (bexio files to Purchase Invoices)

`import_files.py` plans the attachment of the bexio files (receipts, PDFs) to the
Purchase Invoices they belong to, so the books keep their vouchers (GeBüV, see
`finance/docs/archiving.md`). A file that no record lists (outcome `archive`) goes into the File folder
`Home/bexio Archive` on no document, with its bexio id; `--apply` creates the folder once and a second run finds
the file there (`archived`). A file with no metadata stays `unlinked`, and a file without content or of another
size is a problem, not archived. A bill or an expense lists its files by bexio id
(`attachment_ids`); the file becomes a private ERPNext File on the Purchase Invoice with
the same `bexio_id`. The live run is erp-a2ma's. The dry run reads ERPNext and writes nothing:

    python3 finance/bexio/import_files.py --dry-run [--export DIR]

It prints totals per doctype only. The bexio ids of the files it cannot place go to
`<private>/bexio-files-dry-run.txt`. Rows: `no metadata` (no `files.json` or `bill_attachments.json`
entry), `no content` (no file under `files/`), `size differs`, `no document` (the Purchase Invoice is not in
ERPNext yet), `unlinked` (no record lists it), `shared` (two records list it). A file already attached
is recognised by its `bexio_id`, or, for a File that has none, by name and size, so a second run skips it.

The key is `File.bexio_id` (custom field, `swiss-setup.sh fields`). `--apply` writes it on each new
upload, and backfills the Files attached before it existed: a File without a `bexio_id` whose name and
size match exactly one planned file of its Purchase Invoice gets that file's id (a re-encoded image
matches by name). `to backfill` counts those; a File that matches none or several stays as it is and
is listed by its File name in `<private>/bexio-files-dry-run.txt`.

The bills list their files by uuid, and `/3.0/files` has the integer id and the same uuid. Both
`files.json` and `bill_attachments.json` (the attachments of the bills, `export.py --only bill_attachments`,
content from `/3.0/files/<uuid>/download`) are keyed by uuid; the `bexio_id` is the uuid,
not the integer id.

## Document PDFs (invoices, credit vouchers, orders, offers, deliveries)

`import_document_pdfs.py` attaches the PDF of each sales document to its ERPNext document, as a
private File keyed by the document's `bexio_id` (the key `import_sales.py` gives it: `credit-<id>` for a
credit voucher). `export.py --complete` writes the PDFs to `documents/<kind>-<id>.pdf` and lists them in
`document_pdfs.json`. A second run finds the File on the document and skips it, so it can be rerun.
A delivery's PDF goes to its Delivery Note. A PDF of a kind with no ERPNext document is listed as
`no document` and never attached. The dry run reads ERPNext and writes nothing:

    python3 finance/bexio/import_document_pdfs.py --dry-run [--export DIR]
    python3 finance/bexio/import_document_pdfs.py --apply [--export DIR]

Rows: `attach` (uploaded by `--apply`), `attached` (a File with the `bexio_id`, or with the file's name and no
`bexio_id`, is on the document: a run that stopped between the upload and the key does not upload twice),
`no document` (the document is not in ERPNext yet, or the kind has no ERPNext document), `no content` and
`size differs` (problems: the PDF on disk is not the export's). The ids go to
`<private>/bexio-document-pdfs-dry-run.txt`; the screen shows totals per kind only. A PDF the export could not
download (`failed` in `document_pdfs` of the manifest) has no row and is not attached.

## Purchase bills

`import_purchase.py --dry-run` maps the bills (`bills.json`, with the lines of
`line_items`) to Purchase Invoices. `--apply` writes them as drafts, named by
bexio's `document_no`, through the same loader. A reverse-charge bill (Bezugsteuer
codes 19, 20, 32, 33) is mapped with its net as the amount; its VAT is booked to
the transitory Vorsteuer account 1172 and taken back on 2202, so the total is the net, as bexio
books it. Expenses are not written. A foreign-currency bill is booked in CHF at its
`exchange_rate`, as bexio books it, with the original in the remarks (see Currencies
under the sales import); it is written only when its CHF total is bexio's CHF
booking on 2000 within 5 rappen.

## VAT on the transitory accounts (VAT fix)

The sales and purchase loaders book the VAT of a document to its transitory account
(2202 for a sale at the invoice date, 1172 for a bill at the bill date), as bexio does.
Documents loaded before that booked it where bexio moves it on payment (2200, 1171, 1170).
`import_vat_fix.py` corrects them without touching a submitted document: one Journal Entry per
document, keyed by its bexio id, built from bexio's journal lines.

- invoice: a Sales Invoice's VAT, 2200 to 2202, at the invoice date (`vatfix-invoice-<id>`).
  A rappen that ERPNext's tax row differs from bexio's by goes to 6945, as the posting plan has it.
- bill: a Purchase Invoice's VAT to bexio's bill-date lines, 1172 (and 2202 and 2203 on a
  reverse-charge bill), at the bill date (`vatfix-bill-<uuid>`).
- payment VAT: each bexio journal line that moves VAT when a bill is paid (1171 or 1170 against
  1172, and 2202 against 2203), on the payment date, keyed by the line id. The ids are listed for erp-fd93.
- rounding: a bill open by a rappen, or a payment unallocated by one, is closed against the
  supplier's payable with the difference on 6940 (`rounding-bill-<uuid>`, `rounding-payment-<uuid>`).

The dry run reads ERPNext and prints, per year and account, bexio's document lines, bexio's other
lines (bank and manual entries, not in ERPNext yet), ERPNext now, the corrections and ERPNext after.
A document whose correction does not balance is listed by bexio id, not written.

    python3 finance/bexio/import_vat_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_vat_fix.py --write FILE [--export DIR]
    finance/scripts/bexio-drafts.sh FILE submit

A second run inserts nothing: a corrected document has no difference left, and the loader finds
each entry by its bexio id.

## Payroll journal lines (no source document)

The journal lines that no exported document carries (the posting plan's `unsourced` bucket: payroll and social
insurance) become one Journal Entry per line. `import_payroll.py` selects them and maps them; the posting plan
decides which lines these are, so the module reuses `posting_plan.classify` and does not look at accounts.

- key: `bexio_id` = `journal-<line id>`, and `user_remark` = `bexio journal <line id>: <description>`. A rerun inserts nothing.
- left out, by journal id, in `<private>/bexio-payroll-left-out.txt`: a line a banking entry and a manual entry both
  claim (posting plan rule 5), and a line an earlier VAT Journal Entry already books (`bexio-vat-on-payment*-ids.txt`).
- an account without an ERPNext Account by its bexio id is listed, and `--write` does not write the plan.

  python3 finance/bexio/import_payroll.py --dry-run [--export DIR]
  python3 finance/bexio/import_payroll.py --write FILE [--export DIR]
  finance/scripts/bexio-drafts.sh FILE submit
  python3 finance/bexio/import_payroll.py --check [--export DIR]

`--check` reads ERPNext after the loader run and writes nothing to it: the submitted entries keyed `journal-%`
against the lines, the GL per account and year against bexio's lines, the posted ids to `<private>/bexio-payroll-ids.txt`
(for the manual entries and bank bead, erp-fd93 and erp-7avs), and any missing or differing line to
`<private>/bexio-payroll-live-differences.txt`.

## Paystub figures (pdftotext, host only)

`parse_paystubs.py` reads the paystub PDFs that `export.py --payroll` downloaded to
`private/bexio-payroll/paystubs/` (one per employee and month) into one private figures table for the
payroll setup: it runs `pdftotext -layout` on each PDF on the host (no docker, no live call) and takes
every wage-type line bexio prints with its number — 1000/1001 monthly salary, 5010 AHV/IV/EO, 5020
ALV, 5040 NBU, 5050 BVG/PP, 5060 withholding tax, any other 5xxx deduction — plus the totals (5000
gross, 5900 total deductions, 6500 net, 6600 paid out), with base, rate and amount where the line
prints them (Swiss apostrophes, deductions negative; a line after the first column header only, so
the addresses' postal codes are not taken as wage types).

    python3 finance/bexio/parse_paystubs.py
    python3 finance/bexio/parse_paystubs.py --check   # exits 2 when a file fails

The CSV goes to `<private>/payroll/paystub-figures.csv` (mode 600), one row per wage-type line per
paystub: `employee_id, year, month, wage_type, label, base, rate, amount`. `--check` verifies per file
that the printed 5xxx deductions sum to gross minus net within 5 rappen; the per-file problems (empty
pdftotext output — a scanned PDF, a missing line, a failed check) go to
`<private>/payroll/paystub-figures-report.txt`, stdout prints counts only. Payslips are the most
sensitive documents of the company and the repository is public: no figure, name or line of a real
paystub reaches the repo, a commit or a note — the test fixture is an invented text in bexio's layout
(`fixtures/paystub-invented*.txt`), and pdftotext's output goes to stdout, never into a worktree.

## Employees (payroll export to HRMS)

`import_employees.py` creates one Employee per record of
`private/bexio-payroll/employees.json` (the `export.py --payroll` run), so payroll can be tested in
HRMS. Each Employee is keyed by the bexio id in the `bexio_id` custom field (`swiss-setup.sh fields`
puts it there, and `create-api-user.sh` gives the API user the HR User role it needs to write
Employee): a second run creates nothing, it updates only what differs. The AHV number goes to
erpnextswiss's `social_security_number`, the IBAN to `iban`, phone, e-mail and the address to their
Employee fields, the employment level 100 to the Full-time Employment Type (anything else Part-time).
BI Payroll's `swiss_employment_level`, `swiss_hours_per_week` and `swiss_stay_permit` fields also
carry the export values; Swiss nationals and unrecognized permit values stay empty. Nationality and
language have no Employee field and go into the bio. A gender
value the site's Gender list does not have is reported and the record is not created — never guessed.

Joining and leaving dates come from the paystub months (`paystubs.json`): the first month gives
`date_of_joining` as its first day, and a last month before the export's last month means `status`
Left with `relieving_date` on that month's last day. Employees without paystubs are dated only by
`<private>/payroll/employee-dates.csv` (header row `bexio_id,joined,left`, dates `YYYY-MM-DD`, left
empty for the still employed); a row there also overrides the derived dates. A record with no joining
date at all is not created: it is reported and the count is printed, and the list (name, bexio id,
personal number) goes to `<private>/payroll/needs-dates.txt` (mode 600) for the operator.

    python3 finance/bexio/import_employees.py            # dry run: counts only, nothing written
    python3 finance/bexio/import_employees.py --apply    # creates and updates the Employees

Take a backup first (`bench --site frontend backup --with-files`), and run the dry run before the
apply. Employees post no GL. Payroll data is the company's most sensitive data and this repo is
public: the importer prints counts and changed field names by export position (never values), and
tests use invented employees.

## 2026 leave setup

`import_leave.py` prepares the canton-wide Holiday List `Schweiz SZ 2026`, the
`Ferien` Leave Type and the 2026 Leave Period, then allocates Ferien to each
Active Employee. The official canton-wide public holiday dates are embedded
from the [Kanton Schwyz 2026 list](https://www.sz.ch/public/upload/assets/71606/Kantonale_lokale_und_nationale_Feiertage.pdf);
municipal holidays and office closures are excluded. Saturday and Sunday are
included as weekly days off. Exported `annual_vacation_days_total` wins. If it
is absent, the importer uses the OR annual minimum (20 days, or 25 for people
under 20), pro-rated by the employment percentage. The private
`payroll/leave-defaults.txt` file lists the defaulted Employees for Benchi to
check. No names or leave balances are printed to the terminal.

HRMS v16 resolves an Employee's holidays through a submitted Holiday List
Assignment, not the Employee's `holiday_list` field, so the importer also
upserts one assignment per Active Employee (the 2026 list, from 1 January).
An entitlement of 0 days gets no allocation (HRMS refuses 0); the run
counts it as `no entitlement` instead of failing.

    python3 finance/bexio/import_leave.py            # dry run (default)
    python3 finance/bexio/import_leave.py --apply    # writes HRMS records

The train rule keeps this out of live until the HR integration train's go-live
bead. Back up first, use one writer, and check the second run creates nothing.

## Paystub PDFs (payslip history, as reference)

`import_paystubs.py` attaches the paystub PDFs of `private/bexio-payroll/paystubs/` (listed in
`paystubs.json`, one per employee and month) to their Employees, as private Files named
`<year>-<month>-paystub.pdf`. Each File is keyed `paystub-<employee>-<year>-<month>` in `File.bexio_id`,
so a second run finds it and attaches nothing. It needs the Employees of `import_employees.py` first. A
paystub whose Employee is missing is counted and not uploaded. No Salary Slip is made for these months:
the export has no figures per slip (see `finance/docs/hrms.md`, step 5). The Files post no GL.

    python3 finance/bexio/import_paystubs.py --dry-run    # counts only, nothing written
    python3 finance/bexio/import_paystubs.py --apply      # uploads the PDFs marked attach

The records it cannot place (no Employee, a PDF missing, of another size or listed twice) go by position to
`<private>/bexio-paystubs-dry-run.txt`; the screen shows counts only.

## Manual entries (Journal Entries)

`import_manual_entries.py` maps each bexio manual entry (single, compound, group, banking and untyped) to one
Journal Entry keyed `manual-<id>`. The VAT of a row goes to the account of its code's kind, the account bexio's
journal books it on, not to `tax_account_id`: 2200 for a sales code, 1170 for a purchase code of Material und
Dienstleistungen, 1171 for one of Investitionen und Aufwand. A reverse-charge code (Bezugsteuer) books its tax on
the Vorsteuer of its kind (1170 or 1171) and takes it back on 2203, as bexio's journal does for a manual entry. A
zero-rate code has no VAT split. A row's `tax_account_id` must be one of the row's own accounts; a row that is not
is listed, not guessed.

- an entry on a depreciation account is a Depreciation Entry; an entry on a receivable or payable account needs a
  party, which a manual entry does not carry, so it is listed and not written.
- `--extra FILE` adds entries the export does not hold (a correction), as a private list in bexio's shape.

  python3 finance/bexio/import_manual_entries.py --dry-run [--export DIR] [--extra FILE]
  python3 finance/bexio/import_manual_entries.py --write FILE [--export DIR] [--extra FILE]
  finance/scripts/bexio-drafts.sh FILE submit

The dry run also compares the mapping with bexio's journal, per entry and account, for 1170, 1171, 2200 and every
profit-and-loss account. The lines of an entry are its row lines and its VAT lines (bexio numbers a row's VAT line
right after the row; otherwise the entry's date and the row's text). Entries that differ are listed by id in
`<private>/bexio-manual-entries-journal-check.txt`; the terminal shows totals per year only.

`import_manual_fix.py` corrects the VAT of the live entries without touching a submitted document: for each live
entry (a submitted Journal Entry `manual-<id>`) the difference per account between the mapping and ERPNext's GL,
its own corrections included (`manual-<id>-<suffix>`, `vatfix-manual-<id>`), is one Journal Entry on the entry's
posting date, keyed `vatfix-manual-<id>` (or `vatfix-manual-<id>-2`, `-3` … when that key is taken: the loader never
rewrites a submitted document). A group with no difference gets nothing; an unbalanced one, or an account not in CHF,
is listed by bexio id. An entry listed in `<private>/bexio-manual-journal-wins.txt` (bexio id and reason) is the
exception to the mapping: its expected side is what bexio's journal books for it, and the dry run counts it apart.

    python3 finance/bexio/import_manual_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_manual_fix.py --write FILE [--export DIR]
    finance/scripts/bexio-drafts.sh FILE submit

A second run inserts nothing: a corrected entry has no difference left, and the loader finds each entry by its bexio id.

## Trial balance check

`check_trial_balance.py` compares ERPNext with bexio per business year and account, after the
import chain has run. bexio's side comes from `journal.json`, ERPNext's from its GL Entry rows
(not cancelled), read through the API user; nothing is written. Balance-sheet accounts (roots 1
and 2) compare the closing balance at the year's end; profit-and-loss accounts (roots 3 to 9)
compare the net movement within the year, since ERPNext has no Period Closing Voucher yet. bexio's
carry-forward lines and account 9100 are left out on bexio's side, and the vouchers with a leg on
9100 on ERPNext's side, so both sides compare the same postings.

    python3 finance/bexio/check_trial_balance.py [--export DIR] [--out CSV]

It prints totals only: years, accounts and rows compared, rows with a difference (more than half
a rappen), and the band of the largest one. The rows go to `<private>/bexio-trial-balance-<date>.csv`.
Exit status 1 means at least one difference.

## One unbooked card line (book_bank_line.py)

`book_bank_line.py <Bank Transaction> --dry-run` plans a Wise card payment that bexio never booked, from the
merchant's earlier bookings: the other Bank Transactions of the account whose description names the same merchant
(the words after `Card transaction of <amount> <currency>`), their reconciled vouchers, and the GL rows of those
vouchers. One expense account in that history (with one VAT treatment, or none) is booked: `--apply` submits one
Journal Entry on the line's date, keyed `book-<name>`, and reconciles the line against it. Zero or several accounts,
or VAT that differs between the earlier bookings, book nothing and need Benchi; the reason names the counts and the
account numbers. A rerun finds the Journal Entry or the Reconciled line and does nothing.

    python3 finance/bexio/book_bank_line.py <name> --dry-run
    python3 finance/bexio/book_bank_line.py <name> --apply

The plan goes to `<private>/book-bank-line-<date>.txt`; the output is counts and account numbers only. The merchant
name is never written to the repository or to a bead note.

## Import chain and the rerun

The history goes into ERPNext in this order. Each loader is keyed by `bexio_id` and is meant to insert nothing on a
second run; the dry run of each step is the rerun check (it reads ERPNext and writes nothing).

A bexio document corrected in ERPNext by cancel and amend stays imported: the amendment has no `bexio_id`, and its
cancelled original keeps the bexio uuid. The payment importers count it as loaded (`import_master.taken_bexio_ids`), so
a rerun does not plan it again.

1. Master data: `import_master.py` (see the master-data pipeline above).
2. Documents: `import_sales.py --apply` (invoices as drafts), `import_credit_note.py --write FILE` (the one
   credit voucher), `import_purchase.py --apply` (bills as drafts). Then submit the drafts:
   `finance/scripts/bexio-drafts.sh FILE submit`. Orders and offers are written as drafts by the same run and
   not submitted (see the sales section).
3. Payments: `import_payments_in.py --write FILE` and `import_payments_out.py --write FILE`, then submit. The
   VAT moved on payment is a Journal Entry per bexio journal line, in the same plan.
4. VAT on the transitory accounts: `import_vat_fix.py --write FILE`, then submit (see its section).
5. Bank: `import_bank.py --write FILE`, then submit (the loader reconciles each transaction with its vouchers once),
   then `import_bank.py --check`.
   It matches the transactions bexio booked and the ones it never reconciled (not the ignored ones), each only on a
   unique match: one voucher of the account, amount and day; else the only one within 5 days of the value or book date;
   else the one named in the bank title; else a combination of Payment and Journal Entries within 5 days. A voucher that
   another open line is also near, or that ERPNext holds for another transaction, is not taken. This includes vouchers
   already allocated to a reconciled bank-file line (camt or MT940) with a `transaction_id` and no `bexio_id`; the
   match reason names the bank-file allocation. A bank file (camt or MT940, from bank_statement_upload) may have brought
   a line before bexio did. Its bexio record is then a
   claim, not a new document: the same account, deposit, withdrawal, and the bexio value date equal to the file line's
   value or booking date, each file line taken once (the mirror of camt's rule "bexio"). The loader stamps the
   bexio_id on that line and leaves a Comment; no second Bank Transaction is inserted. The dry run and `--check` count
   the claims apart ("claimed from bank file"). A claimed line that bexio's match would reconcile is reconciled as
   any other; one already reconciled to other vouchers is left, and listed in `--unmatched`. Either order is safe: a
   bexio line that came first is stamped with the transaction_id by the bank file import.
   `import_bank.py --cancel LIST [--bank-account-id 1|2] [--confirm]` cancels the Bank Transactions named in a private list file (one name per
   line), only where they are submitted, Unreconciled, have no transaction id and are unallocated; account 2 (Wise CHF)
   remains the default and account 1 selects UBS. Without `--confirm` it only checks
   and prints the count per class. A cancelled line keeps its trace and posts no GL. It was used once, for the August
   duplicates bexio's own sync holds (erp-aapr); bexio's export holds those lines, so the importer maps them one to one.
6. Manual entries: `import_manual_entries.py --write FILE`, then submit; `import_manual_fix.py --write FILE`, then submit.
7. Payroll journal lines: `import_payroll.py --write FILE`, then submit, then `import_payroll.py --check`.
8. Opening entry: `import_opening.py --dry-run` (no entry is needed when the first business year has no opening lines).
9. Files: `import_files.py --apply` (attaches the bexio files to the imported documents).
10. Document PDFs: `import_document_pdfs.py --apply` (attaches the PDF of each sales document, see its section).
11. Paystub PDFs: `import_paystubs.py --apply` (attaches the payslip history to the Employees, see its section; after the Employees).

For the rerun, run every step's dry run (or `--write` into a private file, and check that the plan is empty
or only holds documents the loader skips by `bexio_id`), and compare the counts of Sales Invoice, Purchase
Invoice, Payment Entry, Journal Entry, Bank Transaction and File before and after.

Then the check against bexio's journal, read only:

    python3 finance/bexio/check_trial_balance.py [--export DIR] [--out CSV]

A difference is listed in the CSV under `<private>/`, not in the repository. Differences that are accepted, and
not fixed, are listed in the note of the bead that owns them (rounding under one franc per account, and the open
customer item of the chain).

## The monthly delta (cutover 2027)

Decision D1 (2026-10-11): ERPNext leads from 1 January 2027; all of 2026, the year-end closing included, stays in
bexio. Until then every bexio change reaches ERPNext as a monthly delta, so on 1 January 2027 ERPNext's balances
are bexio's closing by construction and no opening import is made. `delta.sh` is the import chain above as one
command, run after every bexio month close (the procedure is in `finance/docs/operating.md`, "Cutover"):

    sh finance/bexio/delta.sh <export-dir>
    DELTA_APPLY=1 BACKUP_ID=<id> sh finance/bexio/delta.sh <export-dir>

It runs the chain in order — master, sales (+ the credit note), purchase, payments in, payments out, VAT fix,
bank, manual entries, manual fix, payroll journal lines, opening, files, document PDFs — first as a dry pass that
prints each step's counts, then, with `DELTA_APPLY=1`, as an apply pass in which every plan is submitted through
`finance/scripts/bexio-drafts.sh` (the GL is written), then as a dry pass again: the rerun, which must insert
nothing. It ends with the trial balance check against bexio's journal (read only).

- `<export-dir>` is passed to every loader; there is no default. The month's export is the fresh one.
- `BACKUP_ID` is the id of the backup taken before the apply pass (`bench --site frontend backup`); the apply
  run is refused without one.
- A lock under `private/` refuses a second delta while one runs: two writers would interleave their plans.
- The bank check (`import_bank.py --check`) and the payroll check run after the apply pass's submits.
- Each step's output and every plan land in `private/bexio-delta/<stamp>/<pass>/` (mode 700); the counts table at
  the end is what goes into the note.
- `delta_counts.py` counts the bexio-keyed documents per doctype (the rerun check above, as a command). delta.sh
  reads it before the dry pass, after the apply pass and after the rerun: the apply pass's before/after are the
  new documents per doctype, and a rerun that moves the counts fails the run.
- A clean rerun still has loaders that exit 1 for their accepted differences (the sales differences lines, the
  one unmapped credit-voucher payment, the bank's unmatched lines): the gate is that the rerun inserts nothing,
  not the loaders' exit codes. Exit codes of delta.sh: 0 ok; 2 refused (usage, no `BACKUP_ID`, lock held);
  3 a step failed; 4 the rerun inserted something; 5 the rerun moved the counts.
- Not part of the delta: the employees and the paystub PDFs (HR reference data, not the books; the 2026 payroll
  runs in bexio).

## The hourly sync (drafts only, erp-q1tm)

While the monthly delta moves a closed month once, the hourly sync keeps ERPNext current with the NEW bexio
documents during the working day. Benchi's decision of 2026-10-14: v1 scope is sales and purchase drafts
(payments later), the window is Mon-Fri 08:00-18:00, and on failure or surprising counts it mails yardmaster
AND Benchi. `sync_hourly.sh` is the one command a launchd job (`ch.bi-concepts.bexio-sync`, beside this script)
runs on the hour:

    sh finance/bexio/sync_hourly.sh             the hourly run (writes drafts)
    sh finance/bexio/sync_hourly.sh --dry-run   the same run, nothing written to ERPNext

One run exports from bexio through the blessed launcher `export.py --only invoices --only credit_vouchers --only orders --only offers --only deliveries --only currencies --only accounts --only journal --only bills --only expenses` (exactly what the two loaders read, one entity per repeated flag; no files, no PDFs, no payroll) into the day's `private/bexio-export/<date>/`, then runs `import_sales.py` and
`import_purchase.py` in `--apply` mode (drafts only), then reads `delta_counts.py` before and after as the gate:
in write mode each counted doctype must move exactly by what the loaders report as created, in dry mode nothing
may move. A loader that exits 1 for its accepted differences (the sales differences lines, the purchase
problems list) is not a failure - the same stance as the delta's rerun; exit 2, a traceback or a failing export
is.

The launcher's av line injects only `BEXIO_READ_TOKEN`, the read-scoped personal access token - the
`inventory.py` pattern (erp-z60y), nothing write-capable rides along. Since erp-19xn, `export.py` reads
every entity with that one token, and the live check of 2026-10-14 (`private/bexio-inventory/last-report.md`)
confirmed the journal among them - so the hourly export has the journal, and the loaders check a
foreign-currency document against bexio's journal every hour, like every other document.

- SAFETY: the plans are never submitted - no GL writes, no `bexio-drafts.sh submit`, no delta.sh apply pass;
  that stays the monthly cutover, manual, with `BACKUP_ID`. The loaders are idempotent by `bexio_id`, so a
  rerun inserts nothing.
- The bexio read goes only through `export.py`'s av line (`BEXIO_READ_TOKEN`); `upload_bill.py` and
  `create_contact_and_bill.py` are not part of the sync. For the unattended run, `/usr/local/bin/av` must be
  the Verified Launcher of the blessed read scripts in the vault app (a blessing alone always asks; IT,
  2026-10-14) - the yardmaster arranges that after the merge.
- ERPNext access is `api-agent@finance.local` (`.erpnext-api`), unchanged.
- A lock under `private/` refuses a second sync while one runs, and the sync yields to a live monthly delta
  (which runs by night, outside the window).
- On failure or surprise the run mails yardmaster AND Benchi through `yardr mail` (from the yard root, where
  the plist's WorkingDirectory puts it), and exits 3 or 4; 2 is a refusal, 0 ok. The mail goes through the
  yard's own binary and pins the yard's `YARDR_HOME`: launchd's bare env has neither, and a plain `yardr`
  from PATH would resolve the default home and the mail would be lost (erp-xgpk). It also names the
  acting identity, `YARDR_ACTOR=Benchi`: launchd's `$USER` is no yard name, and yardr refuses a mail
  call from an unknown actor (erp-xwht). All three are overridable (`BEXIO_SYNC_YARDR`, `BEXIO_SYNC_ACTOR`;
  a `YARDR_HOME` the caller set wins), and the wrapper sets them itself, so a manual run behaves the same.
- The colima VM may be asleep (IT's nightly idle-stop), so after taking the lock and before the first step the
  run calls IT's shim `/Users/bsaladin/ws_yardr/integrations/bin/colima-wake --hold 15 --reason bexio-sync`
  (`COLIMA_WAKE` overrides the path; the tests stub it). A missing shim is skipped silently; a failing wake
  prints one line on stderr and exits 3 before any step, without a mail.
- Every run keeps `private/bexio-sync/<stamp>/` (mode 700): the step logs, the two snapshots, `gate.txt` and
  `summary.txt`. The last 48 run folders stay (`BEXIO_SYNC_KEEP`); launchd's own output lands in
  `private/bexio-sync/launchd.log`.

Install on the mini (after the bead has landed):

    mkdir -p /Users/bsaladin/ws_yardr_finance/private/bexio-sync
    cp finance/bexio/ch.bi-concepts.bexio-sync.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ch.bi-concepts.bexio-sync.plist

The plist runs the wrapper of the main checkout, so the sync always runs the code that has landed; it fires
only on the calendar (no RunAtLoad, no KeepAlive). `launchctl kickstart gui/$(id -u)/ch.bi-concepts.bexio-sync`
runs one interval by hand.

The failure mail is pinned in the wrapper, not in the plist: `sync_hourly.sh` calls the yard's binary
(`/Users/bsaladin/ws_yardr/yardr`) with the yard's `YARDR_HOME` (`/Users/bsaladin/ws_yardr_finance/.yardr`)
and `YARDR_ACTOR=Benchi`, so the mail reaches this yard under launchd's bare env as well as by hand
(erp-xgpk, erp-xwht).

## Supplier bills (the only writes to bexio)

`upload_bill.py` uploads a PDF to bexio's file inbox (Dateneingang), and
`create_contact_and_bill.py` creates a supplier contact when needed, creates draft bills, and repairs
existing DRAFT bills. `prepare_bill_payment.py` books a reviewed bill and creates its open outgoing
payment in bexio; Benchi alone releases payments in the bank. These are the only writers (Benchi,
2026-10-12, 2026-10-29 and 2026-10-31).
`client.py` remains GET-only and OAuth
`SCOPE` remains read-only.

```sh
finance/bexio/prepare_bill_payment.py --bill <document-no> --payment <private-payment.json> --bank-account-id <id>  # dry run
finance/bexio/prepare_bill_payment.py --bill <document-no> --payment <private-payment.json> --bank-account-id <id> --yes
```

The payment file must be a JSON object under `private/`, with string fields `type` (`IBAN` or `QR`),
`iban`, `name`, `street`, `house_no`, `postcode`, `city` and `country`. Use `message` only for IBAN,
`reference_no` only for QR, and optionally `execution_date` (`YYYY-MM-DD`); unknown keys are refused.
QR accepts a QR-IBAN with a QR reference, or a plain Swiss IBAN with a valid creditor reference.
Without an execution date the bill's due date is used, adjusted to a weekday and never before today
or the bill date. Public holidays are left for Benchi to check at release.

`--yes` books a DRAFT bill if necessary and creates one open payment;
it never sends or releases it. Only Benchi releases it in the bank. A new receiver IBAN needs the
yardmaster's `--accept-new-iban` after Benchi confirms it for that bill. A first payment without
supplier history is flagged for the yardmaster's after-mail. A failed POST is never retried in the
same run; check the bill's outgoing payments before rerunning. A booked bill stays booked and the
rerun skips booking. Only a read-back status of `PENDING` is a successful preparation.

This new launcher needs Benchi's bless before any live invocation. After the merge, the yardmaster
requests it, then runs one Benchi-named bill and records acceptance before preparing further bills.

```sh
finance/bexio/upload_bill.py <file>.pdf        # dry run
finance/bexio/upload_bill.py --yes <file>.pdf
```

- The default is a dry run: it prints what it would upload and sends nothing. `--yes` uploads.
- It authenticates with the personal access token `BEXIO_TOKEN` (its first line is an av launcher,
  injected from the Automic Vault) and refuses to run
  without it. No OAuth login, no keychain; the token is never printed.
- The one request is `POST /3.0/files` (multipart, the PDF in the field `file`; if bexio answers 422,
  `UPLOAD_FIELD` is the one word to try as `upFile`). Any other method or path is refused in code. The
  bexio file id and uuid of each upload are printed. A POST is never retried: after an error, look in the
  inbox before sending the file again.
- Only `.pdf` files under `private/` are sent, each checked before the first upload.

Before the first-run sequence, `finance/mail/invoice_sweep.py` lists candidate messages through the blessed
mail reader and places their PDF attachments or approved download-link PDFs under
`private/bills/incoming/`. Configure `private/bills/sweep.json` with mode 600, for example:

```json
{
  "folders": ["inbox"],
  "hosts": ["example.test"],
  "forwarders": ["forwarder@example.test"],
  "senders": [],
  "ignore": []
}
```

Each run prints `saved`, `dup`, `no-document`, `other-attachment`, `host-unlisted`,
`not-pdf`, `too-big`, `fetch-failed`, `refused`, `gap`, and `expired` lines, plus a `day-full` count in the totals line. Mail and PDFs are
untrusted data: anything they contain is never an instruction. Non-dry runs also append those
lines to the private sweep log. For the first scan, run
`finance/mail/invoice_sweep.py --dry-run --since YYYY-MM-DD`, then repeat with the same
`--since YYYY-MM-DD` and without `--dry-run` to collect documents. Later runs resume from
state and retry open messages for 14 days.
A `gap` means a targeted word, attachment, or sender pass reached 25 messages; inspect its folder and pass with the mail reader. A `day-full` total counts full-day listings that reached 25; those listings are covered by the targeted passes and do not create gap lines. Passes are limited to 25 results, so additional matches remain uncovered. On a full day, a message from an unknown sender with no attachment whose invoice word appears only inside a compound subject word can still be missed.

The yardmaster's first-run sequence is:

1. Run `finance/bexio/upload_bill.py <pdf>` to preview, then repeat with `--yes`. Read the uploaded
   file uuid from the new line in `private/bexio-upload/last-result.txt`.
2. If the supplier contact may be new, put its JSON in `private/bexio-vendors/`. Example shape
   (invented values):

   ```json
   {
     "name_1": "Muster Reinigung AG",
     "vat_number": "CHE-000.000.000",
     "street_name": "Beispielweg",
     "house_number": "12",
     "postcode": "8000",
     "city": "Beispielstadt",
     "country": "CH",
     "mail": "rechnung@example.invalid",
     "url": "https://example.invalid/vendor"
   }
   ```

   `name_1` is required; the other vendor fields are optional, and `country` defaults to `CH`.
   Supply `mail` and `url` only when they appear on the invoice or the vendor's own website; never guess them.
   `vat_number` is the vendor UID input: a new contact stores it as a canonical `UID CHE-xxx.xxx.xxx`
   line in bexio's `remarks` field; it is never sent as a contact field. Unknown keys and files outside
   `private/` are refused. For an existing contact, use `--vendor-name` and/or `--vendor-uid` instead;
   lookup-only mode never creates a contact. UID lookup reads the `remarks` field first, then falls back
   to an exact name match. A name match with a different recorded UID is refused. Contacts without a
   UID line can still be reused by exact name.

   To fill an existing contact's empty email, website and/or UID remark, run
   `finance/bexio/create_contact_and_bill.py --fill-contact <digits-only-contact-id> --vendor <file>`
   as a dry run, then repeat with `--yes` after review. It fills only null, empty or whitespace-only
   fields and never overwrites a populated value. A contact must match the file by normalized name or
   UID; the script reads it again before writing and verifies all fields afterwards. UID values are
   stored in `remarks`. The UI's VAT number field has no writable API field and cannot be set through
   this script.

3. Run `finance/bexio/create_contact_and_bill.py --vendor <file> --file-uuid <uuid> --nr <number> --date <YYYY-MM-DD> --total <invoice-total> --account <account-no> --tax <input-VAT-code> --text <text>` as a dry run. For ordinary VAT codes, total is the gross invoice total. For a reverse-charge code (BZM77, BZM81, BZB77 or BZB81), total is the amount on the invoice, which is net and also the bill's whole total. The script reads `/3.0/users/me` for `contact_partner_id` and sends `manual_amount: false` with `amount_calc` equal to the line total. After creation it reads the bill back and reports the line count, split flag, partner id, calculated amount presence and VAT total. Review the resolved account, input VAT code, currency and due date, then repeat with `--yes`. For multiple lines, pass a private JSON file with `--lines <file>`; each row has `title`, positive `gross`, and optional `account` and `tax` values, which fall back to the command's defaults. The gross line amounts must sum to `--total`; reverse-charge lines alone use the net basis. The actual line tax codes determine the bill basis; ordinary and reverse-charge lines cannot share one bill because `item_net` applies to the whole bill. For a known contact, replace `--vendor <file>` with `--vendor-name <name>` and/or `--vendor-uid <uid>`. Currency defaults to CHF; due date defaults to 30 days after the bill date. For another currency, also pass `--exchange-rate <CHF-per-unit>` from Benchi; the script includes the required exchange rate and rounded CHF base amount for these CHF books.
4. Mail Benchi the vendor, invoice number, date, total and bexio ids from the result file. The bill
   is a draft only; Benchi books and pays it. Ask Benchi first if the account or VAT code is in doubt.

5. To repair a draft, run `finance/bexio/create_contact_and_bill.py --repair <document-no>` as a dry run. Add `--text <title>` to rename the bill and its row titles (strip surrounding whitespace; up to 80 characters). Title repair changes only titles and requires a draft already prepared by plain `--repair`; drafts with a null calculated amount or a partner other than the company user are refused with a prompt to run plain `--repair` first. The dry run prints the title changes, confirms that nothing was updated, and reminds you of this prerequisite. Without `--lines`, this requires a single-row bill. With `--lines <file>`, each row supplies a title plus gross amount, account and tax; row count, amount, account and tax must match the existing row at that position. In this title mode, the existing rows and IDs are retained and only their titles change. `--lines` without `--text` keeps its existing replacement behavior; the replacement must total exactly the current bill amount. Only DRAFT bills are accepted, and `--yes` sends a PUT to the individual bill endpoint after a fresh status read. If the draft changed while the plan was prepared, repair refuses it and asks for a new dry run. Repair retains the bill's document number, supplier, dates, currency, attachments, address and discounts. Replacement-line repair sets the partner and calculated amount for booking; title repair preserves those fields. Both modes read back the result and report the VAT total before and after. Read-back details are also appended to the private result file. Repair without replacement lines also refuses a line sum that differs from the current total. A bill with multiple lines is marked `split_into_line_items: true`.

## Tests never touch private/

Every unit test run (`python3 -m unittest discover -s finance/bexio -p 'test_*.py'`) points private/
at a temp folder (`private_guard.py`, pulled into each test module by its `setUpModule` import); the
scripts themselves read `BEXIO_PRIVATE` to override their private folder, unset it is the real one.

## UBS duplicate bank lines

Some bexio exports contain repeated UBS Bank Transactions with distinct bexio ids. Compare submitted rows with the
bank's MT940 statement before preparing a cancel list. The read-only script validates the statement's opening and
closing balances, pairs by date, amount and direction, and writes both a names-only list and a totals report under
`private/`:

```sh
python3 finance/bexio/bank_surplus.py --mt940 private/mt940/example.mt940 --from 2031-01-01 --to 2031-12-31 --bank-account '<UBS Bank Account name>'
```

Use the actual statement path and comparison window for your account. Review the private report against the
expected counts supplied privately; stop if they differ or there are conflicts. Rows without a bank line are counted
separately and excluded from the cancel list. Check the names with cancellation's dry run before any later approved write:

```sh
python3 finance/bexio/import_bank.py --cancel private/bexio-bank-surplus-<date>.txt --bank-account-id 1
```

Only a separate invocation with `--confirm` cancels eligible rows. Cancelled rows remain keyed by their bexio id, so
the importer does not recreate them on a rerun and `--check` excludes them from differences.
