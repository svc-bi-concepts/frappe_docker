# Swiss setup of company BI Concepts

What the finance site (`frontend`, ERPNext 16.50.0) looks like after the Swiss
setup, how it was done, and how to redo it on a fresh site. Decision for the
app itself: `swiss.md`.

## State

| Item | Count |
| --- | --- |
| Image | `frappe-finance-custom:v16.51.0-swiss-bi13` (all eight ERPNext services; the only `frappe-finance-custom` image at rest after the switch on 2026-10-11; older tags are not kept, see "Switching the stack to a new image") |
| Backup before the bi13 switch | `20261011_001334` (`bench --site frontend backup --with-files`, 00:13) |
| Backup before the bi12 switch | `20261010_234904` (`bench --site frontend backup --with-files`, 23:49) |
| Backup before the bi11 switch | `20261010_184223` (`bench --site frontend backup --with-files`, 18:42) |
| Backup before the bi10 switch | `20261010_131236` (`bench --site frontend backup --with-files`, 13:12) |
| Backup before the bi9 switch | `20261010_122454` (`bench --site frontend backup --with-files`, 12:24) |
| Backup before the bi8 switch | `20261010_114102` (`bench --site frontend backup --with-files`, 11:41; bi8 carries the Treasury workspace on the module folder) |
| Backup before the bi7 switch | `20261010_113546` (`bench --site frontend backup --with-files`, 11:35) |
| Apps | frappe 16.51.0 on the live site (bi13, from `build-image.sh` with `FRAPPE_TAG=v16.51.0`), erpnext 16.50.0, erpnextswiss 1.34.1, bi_finance 0.0.1, hrms 16.50.0, bi_payroll 0.0.1 (hrms and bi_payroll installed on the live site on 2026-10-10, after the bi11 switch: `install-app` each, `migrate` twice, the second migrate a no-op; Decision 2026-10-10, `hrms.md`) |
| Monthly patch updates (D4) | `operating.md`, "Monthly update (D4)" |
| Wise sync (`Wise Settings`) | `sync_enabled` 1, so the hourly job `bi_finance.wise.sync_scheduled` runs; `backfill_from` 2026-10-01. Feed rows before October are not fed by the hourly job. The matcher pairs a bexio line with a feed row of the same amount up to 4 days later (card payments also within 1 percent when the merchant words agree), and `sync_now` takes a one-off `start` and `end` for the backfill (erp-hq39): 2026-02 to 2026-06 have no bexio lines and are created by one run with end 2026-06-30; for July to September, where bexio's lines are kept, the feed creates nothing (a month's Bank Transaction net already equals the GL) and the unmatched report for those months is information only. First live sync (bi12, 2026-10-11): 5 Bank Transactions on Wise CHF, all October, GL Entry count unchanged; the rerun creates 0. The balance check does not agree yet, because the months before October are not in the feed; expected until erp-hq39 |
| Accounts, company BI Concepts | 179 (KMU chart, 9 roots, numbers in the names) |
| Sales Taxes and Charges Templates | 15 (bexio codes) |
| Purchase Taxes and Charges Templates | 27 (bexio codes) |
| Item Tax Templates | 42 (bexio codes) |
| Fiscal years | 12 (2015 to 2026), each linked to BI Concepts |
| Custom field `bexio_id` | 21 doctypes |

The 3+3 stock templates (`Switzerland normal/reduced/lodging VAT`) are gone.
The QR-bill print format of erpnextswiss is not enabled (it sends invoice
data to data.libracore.ch; separate decision, see `swiss.md`, Risks).

## What was done, in order

1. Checked that the chart could still be replaced: `GL Entry` count 0.
2. Backup: `bench --site frontend backup --with-files`. Files, in
   `sites/frontend/private/backups/` inside the `sites` volume:
   `20261009_203105-frontend-database.sql.gz`,
   `20261009_203105-frontend-site_config_backup.json`,
   `20261009_203105-frontend-files.tar`,
   `20261009_203105-frontend-private-files.tar`.
3. Image: `finance-local.yml` now sets `image: frappe-finance-custom:v16.50.0-swiss`
   on the eight services that ran `frappe/erpnext` (the override keeps
   `pwd.yml` as upstream has it, so merging upstream stays clean).
4. `docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d`,
   then `bench --site frontend install-app erpnextswiss` and
   `bench --site frontend migrate`.
5. `finance/scripts/swiss-setup.sh` (steps `coa`, `vat`, `fiscal`, `fields`).

## Desk conventions

Labels are in English, with the Swiss term where the ESTV or the paystub uses it: the MWST Ziffer numbers in
brackets ("Normal (302)"), Quellensteuer, BVG, Bezugsteuer. Titles and series: a VAT Declaration is titled
`MWST <period>` (`MWST 2026Q3`, `MWST 2026H1`), an ERPNext invoice keeps its `ACC-SINV` name, and a bexio number
stays as bexio has it (in its own field, never renamed). Where a thing lives: the Swiss steps are on the Treasury
workspace's cards, one per area (Banking, Payments, MWST, Cash forecast, Payroll), and nowhere else is a finance step only reachable by search. What a button does before
it writes: every write has a preview or a dry run first (Fill from payments shows the figures, then writes only the
draft on its own button); a submitted document is never changed by a button, and Download Transfer File only reads.
See `finance/docs/operating.md` for the steps in the order they are done.

The rail: ERPNext's rail has three companions, each on ERPNext's own rail with no apps-screen tile of its
own: bi_finance's Treasury, bi_payroll's Swiss Payroll, and hrms's HR modules (HR Setup to Tax & Benefits,
a one-key patch to hrms, `finance/docs/erpnextswiss-patches.md`). They follow ERPNext's entries in the
order the apps are installed (`sites/apps.txt`: bi_finance, bi_payroll, erpnext, erpnextswiss, frappe, hrms).
The order, and hiding an entry, is arranged in the Desk with a Workspace Manager (the site layer, no code).

BI Finance is a companion of ERPNext, not an app of its own in the apps screen (Benchi, 2026-10-12). Its
shipped documents are a Dock mounted on erpnext (`dock/bi_finance/bi_finance.json`: one rail entry, Treasury, after
Portal) and a module Sidebar titled Treasury (`bi_finance/sidebar/treasury/treasury.json`), arranged like
ERPNext's Accounts: Home, the daily documents at top level with icons, then the Reports, Banking and Setup sections.
The claim rule: a Sidebar item carries `is_default_module` when it is a document Benchi works in daily that its own
app does not navigate, so a bare route to it (a workspace card, a search result, the Add button of a list) opens in
Treasury. VAT Declaration and Payment Proposal are claimed for that reason, since erpnextswiss's shell would
otherwise open them. ERPNext's own documents are never claimed (its bank rows stay in ERPNext's shell). A new doctype
or report of ours goes into the sidebar, and into the top level if it is daily work.

## The setup script

`finance/scripts/swiss-setup.sh [coa] [accounts] [vat] [fiscal] [fields] [currencies] [banks] [gebuev] [qrbill] [host] [payments] [hr]`
pipes `swiss-setup.py` into bench's Python in the backend container. No argument
runs all steps. Every step skips what exists, so a second run changes nothing.

**qrbill.** Creates or updates the print format "BI Sales Invoice QR" from
`finance/apps/bi_finance/bi_finance/bi_sales_invoice_qr.html`, the file the
image installs. The QR code comes from the app's `qrbill.py`, drawn on the
server with pyqrcode (Frappe's own package), so no invoice data leaves it.
The format is the default print format of Sales Invoice (Benchi's word,
2026-10-11): the Property Setter `Sales Invoice-main-default_print_format`
is a fixture of bi_finance, and this step sets it when it is missing, so a
fresh site gets it from the step and an existing site keeps it through a
migrate. The slip is 105 mm at the foot of the last page, whatever the
number of pages: a short invoice keeps it on page 1, a long one gets it at the foot of
its last page, or on a page of its own when the body leaves less than 105 mm. The page
cannot measure its body (wkhtmltopdf runs with JavaScript off), so the format renders
twice. The first render has the body alone with a marker after it; pypdf reads the
marker's height on the last page (`qrbill.py` `sales_invoice_slip_spacer`), and the
second has a gap of that height before the slip. That is one more wkhtmltopdf run per
print, also for the HTML preview. The gap assumes A4 with the format's 15 mm top and
1 mm bottom margins. The margins are set in the format's CSS: wkhtmltopdf reads
`.print-format` margins, not the format's margin fields. The IBAN prints in groups of
four; the QR text does not. The slip's labels follow the Sales Invoice's language (SIX: one language per slip): de, fr, it or en by its first two letters, German for any other or an empty one (`qrbill.py` `SLIP_LABELS`, `slip_labels`), and the invoice number line of the slip and of the QR text follows them too. The QR payload carries a SCOR creditor reference (ISO 11649)
built from the invoice number (`ACC-SINV-2026-00001` → `RF38ACCSINV202600001`): the slip
prints it as "Referenz" in groups of four in the receipt and the payment part, and the
bank's camt.054 carries it back, so a payment can be matched to its invoice (planned,
erp-f5ss). A number that leaves more than 21 characters after the strip is refused. The
payload passed the official swiss-qr-bill.org validator with no findings (see
`finance/docs/swiss.md`, Risks 2). After every migrate the app's `after_migrate` hook
(`bi_finance/setup.py`) disables the erpnextswiss print formats whose HTML names
`data.libracore.ch`, so erpnextswiss's own fixtures cannot turn them back on.
**host.** Sets the site's `host_name` from the `HOST_NAME` environment variable, the
URL the containers can reach (`https://<machine>.<tailnet>.ts.net:8448`, the tailnet
name of the host). Without it, `wkhtmltopdf` fetches `/assets` from `frontend` port 80,
where nothing listens, and every PDF on the site fails with `ConnectionRefusedError`.
The name stays out of the repo, so it is passed on the command line:

```sh
HOST_NAME=https://<machine>.<tailnet>.ts.net:8448 finance/scripts/swiss-setup.sh host
```

The URL is also what mails and prints link to. Check it from the backend and the
queue containers (`curl .../api/method/ping` gives 200) before relying on it.

**coa.** Stops with exit 3 if the company has GL entries. Otherwise it deletes
the 3+3 templates, clears the company's account links and the Mode of Payment
accounts, deletes the company's accounts, and creates the accounts of
erpnextswiss's `coa_import/accounts_template.csv` (Kontenrahmen KMU, 179
rows). The company defaults named in the CSV are set (receivable 1100,
payable 2000, income 3200, expense 4200, cash 1000, round-off 6940, ...);
five defaults have no field in v16 and are skipped with a message. Account
2200 Umsatzsteuer gets account type `Tax`, which the CSV leaves empty and the
tax templates need. Mode of Payment `Cash` points at 1000 Kasse.
erpnextswiss's own importer is not used: it imports `pandas`, which the image
does not have (bead erp-p1xb).

**accounts.** The bexio additions the payroll books into — 2270 KK
Vorsorgeeinrichtung, 2271 KK AHV, IV, EO, ALV, 2273 KK Unfallversicherung,
2274 KK Krankentaggeld, 2279 KK Quellensteuer, 5003 Provisionen, 5720
BVG-Beitrag (`bexio-mapping.md`'s chart rows; read off the live chart
read-only, numbers and labels only). erpnextswiss's template has none of them,
so a fresh site's migrate fails on bi_payroll's account fixtures without them.
The step creates each missing one under the group the live chart hangs it
under (the 227x under root 2, 5003 under 50, 5720 under 57), named
`<number> - <name> - <abbr>` like every account, skips what exists, and
refuses a missing parent group instead of making one. On a fresh site the
order is `coa`, `accounts`, then the migrate that takes the fixtures. The
table is one source with `bi_payroll.test_payroll_accounts`'s `BEXIO_ADDED`:
a test compares the two. The live chart already has all seven (the history
import made them), so on live the step creates nothing; the HR 6 parallel
payroll is proven on the copy, which is restored from a live backup and so
has them too.

**vat.** bexio's 42 VAT codes (`/3.0/taxes`), 1:1, replace the templates
ERPNext had. Each code is one template per kind, named after the code:
`UN81 8.1% Normalsatz`, `VM81 8.1% Normalsatz Material/DL`,
`BZB81 8.1% Bezugsteuer Invest./Aufwand`. The bexio id is in `bexio_id`
(unique) on all three template doctypes, and the importers map a document's tax
by that id, never by its rate. The template's description keeps bexio's period
(`bexio 28, gültig ab 2023-07`); a code that is inactive in bexio is disabled.

- Sales (15): `UN77`, `UN81`, `UR25`, `UR26`, `US37`, `US38`, `UEX`, `ULA`,
  `MEL`, `UNO`, `SUB`, `SPE`, `UO77`, `UO81`, `U00`. Booking to 2202, bexio's
  transitory account: the VAT is moved to 2200 when the invoice is paid.
- Purchase (27): `VM…`, `VIM`, `ZOLLM`, `BZM…` and `VB…`, `V00`, `VSF`, `ZOLLB`,
  `BZB…` book to the transitory Vorsteuer 1172 at the bill date (bexio's account;
  moved to 1170 or 1171 when the bill is paid); corrections `VES`, `VEV`, `VKÜ`
  (1172, 1173, 1174), one per bexio id, for 7.7 and 8.1.
- Item Tax (42): one per code, one row on the code's account.
- Defaults: `UN81` (sales) and `VM81` (purchase, Material/DL).

How the special codes are modelled:

- **Bezugsteuer** (`BZ…`, reverse charge): one purchase template with two rows,
  +rate on 1172 (Vorsteuer) and -rate on 2202 (the liability bexio books the
  reverse charge on). The net is 0.
- **Einfuhrsteuer** (`ZOLLM`, `ZOLLB`): rate 0. The import VAT is on the customs
  document; ERPNext does not deduct it from a percentage. Booking it is part of
  the import (open question in `bexio-mapping.md`).
- **Corrections** (`VES`, `VEV`, `VKÜ`): inactive in bexio. Booked as journal
  entries, so the templates exist for reference only. Their accounts (1172 to
  1174, by the order of the mapping) are not verified.
- Accounts of the other codes follow the kind (sales 2202, purchase 1172). The
  bexio journal books them so; `vat --check` compares the export's own accounts.
- Account types: 1172 to 1174, 2202 and 2203 are set to `Tax`, as 2200 is in `coa`:
  an Item Tax row takes only accounts of that type.

Retired: the ERPNext templates `USt…`, `VSt…` and `MWST…` (the 8 %-period
names of the first setup). `vat` deletes them once nothing refers to them; a
template still linked by an item or a document stops the step.

`swiss-setup.sh vat --check <export dir>` compares the export's `taxes.json`
and `accounts.json` with the templates (code, rate, account, active) and prints
each difference; it exits 1 on any. Needs the full export with `/3.0/taxes`.

Checked on a draft Sales Invoice (not saved) with the former 8.1 % template:
100.00 net gives 8.10 tax on 2200 and 108.10 total. The same numbers apply to
`UN81`.

Method: ERPNext books VAT on the invoice date, on 2202 (sales) and 1172 (bills).
bexio declares on payments received (vereinnahmte Entgelte): the VAT moves to
2200 (sales), 1170 or 1171 (purchases) on the payment date. ERPNext makes the
same moves as one Journal Entry per bexio journal line, on that date. So the
VAT accounts 2200, 1170, 1171 and 2203 are the payment basis, and they agree
with bexio's journal per quarter (`finance/bexio/mwst_compare.py`, read only). The net of
these accounts is zero in a settled quarter, so the checks compare the flows (the net of
both sides of the lines, without the settlement) and the settlement entry (`swiss.md`,
"VAT per quarter against bexio").
The form's figures per rate on that basis are built by `mwst_report.py
--basis payment`, from the Payment Entries of the period, each split over the
invoices it pays; the per-rate Ziffern are compared with bexio's per quarter,
with rounding and journal-mirror differences only
(see `swiss.md`, "VAT per quarter against bexio").

**fiscal.** Calendar years 2015 to 2026. 2026 existed; the others were added,
and each gets a row for BI Concepts.

**fields.** Custom field `bexio_id` (label "bexio ID", Data, unique, read
only, in standard filter, not copied with a document) on Customer, Supplier,
Contact, Address, Item, Account, Sales Invoice, Purchase Invoice, Journal Entry
and Payment Entry, and the master, document and tax-template doctypes after
them, File included (the bexio uuid of an attached file, see the Attachments
section of `finance/bexio/README.md`), placed after the name or title field where the doctype has
one. Read only blocks the form, not API or Data Import writes. Unique means two
documents with an empty id are fine; a second document with the same id is
rejected (checked, `UniqueValidationError`).

**gebuev.** The GeBüV safeguards (OR 958f): posted records cannot change or
disappear silently. Each item checks the current value, sets it if needed, and
prints what changed. A second run changes nothing.

- Accounts Settings "Enable Immutable Ledger" on. Cancelling a document then
  posts reversal entries and leaves the original GL rows unchanged. Without it,
  cancelling sets `is_cancelled` on the original rows in place.
- Accounts Settings "Delete Accounting and Stock Ledger entries on deletion of
  transaction" off (already off). Deleting a document keeps its ledger entries.
- Delete right: on Account, Customer, Supplier, Item, Bank Account, Company,
  Sales Invoice, Purchase Invoice, Journal Entry, Payment Entry and Bank
  Transaction only Accounts Manager keeps it. Other roles lose it, so
  api-agent loses delete on all of them. Frappe already refuses to delete a
  submitted document, for everyone, so this covers drafts and cancelled ones.
  Gap: Accounts Manager can still delete a cancelled document; closing that
  needs a server hook or removing the right from Accounts Manager.
- Track Changes (Version) on the same doctypes (a Property Setter, so a
  migrate keeps it). They were already on.

Not in the step: v16 has no separate audit-trail setting; Track Changes is the
version history. Freezing is a separate command, below.

**freeze.** `swiss-setup.sh freeze <YYYY-MM-DD> [--apply]` sets the company's
"Accounts Frozen Till Date": no posting dated on or before the date. It is a dry
run unless `--apply` is given. It shows the current freeze, the GL entries that
the date would cover, and the role allowed to post into frozen periods (set by
hand; the command does not set it). It refuses to move a freeze back. Not run
yet: the history import comes first.

**hr.** The HR roles and the HR settings (HR 5, erp-mvr9). The user is named by
the `HR_USER` environment variable, never in this repo:

```sh
HR_USER=<user> finance/scripts/swiss-setup.sh hr
```

Without `HR_USER` the step says so and skips, like `host` without `HOST_NAME`, so a run
of all steps does not stop on it.

The step gives the user the roles HR Manager, HR User and Leave Approver — the
ones HRMS v16.50.0 installs (role profile "HR" in `hrms/setup.py`, together with
Expense Approver). A fourth goal role, **Payroll Manager, does not exist in
HRMS v16.50.0**: the payroll doctypes (Salary Slip, Payroll Entry) belong to HR
Manager. The step grants it only when the site has the Role document, and never
creates one, so a later HRMS that ships it is picked up by a rerun.

Payroll Settings: `payroll_based_on` = Leave (not attendance),
`include_holidays_in_total_working_days` = 1, `email_salary_slip_to_employee`
= 0 — Salary Slip's `on_submit` mails the slip to the employee exactly when that
check is on, so off means no slip e-mail on submit and none by hand. HRMS
v16.50.0 has **no rounding setting**: CHF rounds to 0.05 through the Currency
smallest fraction, already set by the `currencies` step.

HR Settings: v16.50.0 has **no switch** for employee self-service or for
onboarding e-mails, and the step invents none. Self-service is off because no
enabled user holds the "Employee Self Service" role and no Employee carries a
user; onboarding e-mails go only from Employee Onboarding documents, of which
the site has none (the importers write Employees directly). `hr --check`
reports the counts:

```sh
finance/scripts/swiss-setup.sh hr --check
```

prints every enabled user that holds an HR or payroll role (HR Manager, HR
User, Leave Approver, Expense Approver, Payroll Manager, Employee Self
Service), the two settings' values, the employees that carry a user and the
Employee Onboarding count. With `HR_USER` set it also compares the wanted state
and exits 1 on any difference. The API user keeps System Manager and its
existing roles; the step touches only `HR_USER` (erp-wnx7). Live run and the
rerun on the copy are the go-live bead's (train rule): backup first, one
writer, read back, rerun 0.

**Treasury.** The workspace "Treasury" is a file of bi_finance, as are its number cards
(`bi_finance/bi_finance/number_card/`: the total in CHF, and one card per Bank or Cash account by its
chart number) and the month-end chart (`bi_finance/bi_finance/dashboard_chart/`). The image's migrate syncs them, so
nothing is made by hand on the site. A new bank account gets its card as a new file. The
cards read the report "Cash Position" (`bi_finance/bi_finance/report/cash_position`), which reads the GL, not Bank
Transactions. A foreign-currency balance is turned into CHF at the Currency Exchange rate of
the as-of date, or the latest one before it. ERPNext fills that table from its rate source
(Currency Exchange Settings, frankfurter.dev); the report never fetches a rate. A missing
rate leaves that account's CHF value out, and the report says so. The hooks of bi_finance
list the two doctypes (`importable_doctypes`), since Frappe syncs only its own cards and
charts from the module folders. Those folders (`workspace/`, `number_card/`,
`dashboard_chart/`, `report/`, `doctype/`) sit in `finance/apps/bi_finance/bi_finance/bi_finance/`:
Frappe reads module "BI Finance" from `<package>/bi_finance`. In bi7 they sat one level higher,
and migrate found no Treasury; bi8 moves them (`test_treasury`, Layout, checks the place).

## Building an image

`finance/scripts/build-image.sh live|copy <tag>` builds the base layer, the `bi_finance`
layer and the `bi_payroll` layer, each with `docker buildx build --load` on IT's capped
builder (buildx builder `capped`, a 1.2 GiB hard cap including swap). The capped builder
only: a plain `docker build` runs on the default builder and shares the mini's memory
with the live stack — on 2026-10-11 that OOM-killed the live containers twice. IT's rule
after that: never build with the default builder on the mini.

The script refuses to run when the builder does not exist (it checks `docker buildx
inspect capped` first); if that check fails, ask IT. `BUILDER=<name>` points the script
at another builder — on a machine without IT's setup, name that machine's own capped
builder, never the default.

If docker itself answers nothing, the socket is gone: run IT's colima-sock-fix, then
retry. The asset build's node heap is capped with `NODE_OPTIONS=--max-old-space-size=$NODE_HEAP_MB`
(the script passes it into `images/custom/Containerfile`); `NODE_HEAP_MB` defaults to 900. The
cap bounds only the V8 heap, so the whole node process has to fit the builder's budget. It only
counts since `finance/patches/frappe`: frappe's bundler replaced any `NODE_OPTIONS` with 75% of
the RAM it sees, which on 2026-10-11 got the bi14 hrms vite step SIGKILLed (status 137) on the
capped builders. The Containerfile therefore runs `bench init --skip-assets`, applies that patch
and then `bench build --production`. A different value is an env setting,
`NODE_HEAP_MB=1536 finance/scripts/build-image.sh live <tag>`, and must be a number.
The script needs free disk for the scratch layouts (about the size of the image, twice).

## Switching the stack to a new image

Recreating `backend` or the queue containers gives them new IPs. nginx in
`frontend` keeps the old backend IP, so ERPNext answers 502 until it is
restarted (down about 10 minutes on 2026-10-09 at 22:51). After every
recreate, restart `frontend` and `websocket` and check the ping:

```sh
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance restart frontend websocket
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/method/ping   # must print 200
```

After a VM restart (colima, a reboot of the Mac mini) the containers come back on their own:
`restart: unless-stopped` is on the long-running services in `pwd.yml` (db, redis, backend,
frontend, websocket, queue-short, queue-long, scheduler), so a clean stop does not leave them
down. nginx in `frontend` may still keep the backend IP from before the restart, so the same
ping can answer 502. If it does, restart `frontend` and `websocket` as above and check the ping
again; no recreate is needed.

Back up first (`bench --site frontend backup --with-files`), and switch only
when no other session is writing to ERPNext. A deploy runs the switch script,
which does the restart and the ping, then the clean-up:

```sh
finance/scripts/switch-image.sh <new-tag>
```

It reads the previous tag from `finance-local.yml`, sets the new one on all eight
services, brings the stack up, restarts `frontend` and `websocket`, and waits for
ping 200. On 200 it removes the image the stack ran before, and the new image's
`-base` layer. If ping does not answer, it sets `finance-local.yml` back to the
previous tag, brings the stack up on it and keeps the old image; the new image
stays, and a person removes it.

**One image at rest:** only one `frappe-finance-custom` image is kept, the one the
stack runs. A build leaves two tags (`<tag>-base` and `<tag>`) until a switch
removes the base. A switch-free build (a copy for a test, say) is removed by hand
once it is no longer needed: `docker rmi frappe-finance-custom:<tag>` and
`frappe-finance-custom:<tag>-base`.

A change to `bi_finance` needs the full build (`build-image.sh`, a new tag). There is no
one-layer shortcut: IT's capped builder cannot read the local image store, so a layer
cannot be built `FROM` a tag that exists only there. The script gets around that inside
one run, writing each layer as an OCI layout in a scratch directory (removed at the end)
that the next layer takes as its base.

## Redo on a fresh site

The live image is built from `finance/apps.json` (with HRMS, and `bi_payroll` on top of
`bi_finance`), under a new tag that does not exist yet (the script refuses an existing one).
`finance-local.yml` names the tag in use:

```sh
finance/scripts/build-image.sh live <new-tag>
```

With the swiss image built (`finance/scripts/build-image.sh`), the stack
running on it and company BI Concepts (CH, CHF) created:

```sh
docker compose -p frappe-finance exec -T backend bench --site frontend install-app erpnextswiss
# install-app bi_finance fails with "App bi_finance not in apps.txt" unless sites/apps.txt
# lists it; the image's apps.txt does not (the live volume's does). Add the line first.
docker compose -p frappe-finance exec -T backend sh -c 'grep -qx bi_finance sites/apps.txt || echo bi_finance >> sites/apps.txt'
docker compose -p frappe-finance exec -T backend bench --site frontend install-app bi_finance
docker compose -p frappe-finance exec -T backend bench --site frontend migrate
finance/scripts/swiss-setup.sh
```

`bi_finance` (finance/apps/bi_finance) is this repo's app for the QR-bill. It is
not in `finance/apps.json` (bench clones apps from a git URL, and this one is
not pushed): `finance/scripts/build-image.sh` builds it as a layer on top of
the image. Its offline tests run in the image:

```sh
docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/frappe-bench/apps/bi_finance:ro" \
    frappe-finance-custom:v16.50.0-swiss-bi9 \
    sh -c 'cd /home/frappe/frappe-bench/apps/bi_finance && ../../env/bin/python -m unittest bi_finance.test_qrbill'
```

The Cash Position report has its own offline tests, in the same way (`bi_finance.test_cash_position`).
The whole package runs with one command, the one to run before a test line (0 errors, 0 failures):

```sh
docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/frappe-bench/apps/bi_finance:ro" \
    frappe-finance-custom:v16.50.0-swiss-bi9 \
    sh -c 'cd /home/frappe/frappe-bench/apps/bi_finance && ../../env/bin/python -m unittest discover -s bi_finance -t .'
```

The setup script's pure parts are tested without a container:
`python3 -m unittest discover -s finance/scripts -p 'test_*.py'`.

On a site with transactions the `coa` step refuses; run `vat fiscal fields`
only.

## Bexio history

The bexio accounting history is in ERPNext, from the first business year to the
current one, loaded in the order of the import chain (`finance/bexio/README.md`,
"Import chain and the rerun"). Each record carries its bexio id in `bexio_id`,
the key that makes a rerun skip it.

How it was checked: `finance/bexio/check_trial_balance.py` compares, per business
year and account, bexio's journal with ERPNext's GL Entry rows (read only, nothing
is written). Balance-sheet accounts compare the closing balance, profit-and-loss
accounts the net movement of the year. The rows are kept outside the repository.

Result of the check on 2026-10-10: 8 business years, 88 accounts, 308
year-account rows compared. 24 rows differ, in 12 accounts:

- 20 rows are rounding of up to 3 rappen from ERPNext's 0.05 rounding of the
  imported invoices. Accepted, not fixed.
- 4 rows come from one open item: a 2024 sale that bexio booked without a
  customer. It stays open until the customer is named; then one Journal Entry
  with the customer as party posts it, and the check runs again.

The rerun of the chain changes nothing: each loader's dry run reports nothing to
write, and the counts of Sales Invoice, Purchase Invoice, Payment Entry, Journal
Entry, Bank Transaction and File are the same before and after. Orders and offers
are not part of the history and are not loaded.

Not reconciled, and not in the GL: 34 bank transactions in a foreign currency
(no GL entry is made for them), and 73 bank transactions that are not reconciled
with a voucher. Both wait for confirmation.

Not done here: no period is locked or frozen. The MWST per quarter against bexio
is in `swiss.md`, "VAT per quarter against bexio".

### Cutover 2027

Decision D1 (2026-10-11): ERPNext leads from 1 January 2027. Until then the books stay level with bexio
through the monthly delta (`finance/bexio/delta.sh`, the procedure in `finance/docs/operating.md`,
"Cutover"): after each bexio month close — October, November, December 2026, then after the 2026
Abschlussbuchungen — the delta's apply pass brings ERPNext back level, and the rerun must insert nothing.

The 31.12.2026 check: after the closing delta, `check_trial_balance.py` compares ERPNext's closing balances
per account with bexio's. Result: (pending, January 2027 — this paragraph is written when the check has run;
everything must be explained by the known rows above, and ERPNext's 1.1.2027 opening is then bexio's closing
by construction, with no opening import.)
