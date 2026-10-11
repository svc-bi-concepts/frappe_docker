# Operating the finance stack: month, quarter, payroll

One page of the recurring work, in the order it is done. Every Swiss step is on the Treasury
workspace's **Switzerland** card (Desk: Treasury, then Switzerland). The Desk place is in bold; the
document that explains the step is in brackets. Conventions for labels and buttons:
`finance/docs/erpnext-setup.md`, "Desk conventions".

## Month

1. **Wise and PayPal:** they sync each hour by themselves. Check on **Wise Settings** and
   **PayPal Settings** (Switzerland card) that the hourly sync is on. [`finance/apps/bi_finance/README.md`]
2. **Bank Statement Upload:** upload the UBS camt file of the month (**Bank Statement Upload**,
   Switzerland card). [`finance/apps/bi_finance/README.md`, bank_statement_upload]
3. **Unreconciled bank lines:** clear what is left (Treasury shortcut). [`finance/docs/payment-runs.md`, the camt match]
   After each Wise sync and camt import the matching job reconciles the lines it is certain of (the
   creditor reference of an open Sales Invoice, a payment run's EndToEndId, one open invoice or bill of
   the named party within 5 days, a Bank Transaction Rule) and leaves the rest here. It runs only when
   **Bank Matching Settings** has *enabled* on; rule bookings need *rules_enabled* too (both start off).
   A payment run of several invoices to one supplier writes their numbers joined into one EndToEndId, so
   that line is not matched by it and stays for the Bank Reconciliation Tool. The month-end Exchange Rate
   Revaluation arrives as a draft on the 1st (only when an account has a gain or loss): review and submit
   it; the 31.12 one needs the ESTV year-end rate entered as a Currency Exchange row dated 31.12 first.
4. **Bank Reconciliation Tool:** match the remaining lines to Payment Entries (Treasury shortcut).
5. **Payment Proposal:** create for the due bills (Switzerland card), check the list and the total,
   submit, then **Download bank file**. Upload that file in UBS e-banking; ERPNext sends nothing to a bank.
   [`finance/docs/payment-runs.md`, Run]

## Quarter

1. **MWST pro Quartal vs bexio:** compare the quarter's Ziffern with bexio. Not yet in ERPNext: the page
   comes with erp-skbk. Until then the comparison is `finance/bexio/mwst_report.py --basis payment`,
   written to a private file. [`finance/docs/swiss.md`, VAT per quarter against bexio]
2. **VAT Declaration, new draft:** Switzerland card, VAT Declaration, New. Start and End are the quarter
   (or the half-year, e.g. 2026H1); the title becomes `MWST <period>`.
3. **Fill from payments:** press it on the draft. The dialog shows the figures first; **Write draft**
   writes the draft, nothing is submitted. A submitted declaration of the period is never changed.
   [`finance/docs/swiss.md`, The VAT Declaration]
4. **Check:** compare the form with step 1. Recalculate is safe. The Umsatz rows 220 to 235 are not
   filled by the fill: a quarter with exports or exempt supplies is not filed until that is settled.
5. **Submit**, then **Download Transfer File** on the submitted declaration: the estv.xml (eCH-0217 2.0.0).
6. **ePortal upload:** Benchi uploads the XML at the ESTV portal. Do not press **Get values**: it reads the
   invoice basis and would overwrite the payment-basis figures.

## Payroll month

1. **Payroll Entry or Salary Slips:** run the month's payroll in HRMS (HR, Payroll Entry), then check the
   submitted Salary Slips. [`finance/docs/hrms.md`]
2. **Payroll Swiss Settings:** the rates the slips use (Switzerland card). Check them against the
   paystub of the month. [`finance/apps/bi_payroll/README.md`]
3. **QST Tariff:** the Quellensteuer codes and their rows (Switzerland card) for employees who are
   taxed at source.
4. **Payroll Hand-over:** the report of the month per employee, gross, net and the Swiss components, for
   the trustee (Switzerland card). It reads the submitted slips; it changes nothing.
5. **Still manual until erp-1jh5:** the BVG, UVG, NBU, KTG and FAK rates and the BVG insured salaries are
   placeholders until the fund and insurer sheets are in. Until then the slips are checked against the
   paystubs by hand.

## Cutover (bexio to ERPNext, 2027)

Decision D1 (2026-10-11): ERPNext leads from 1 January 2027. All of 2026 stays in bexio, the year-end closing
included; ERPNext runs in parallel and receives every bexio change as a monthly delta
(`finance/bexio/README.md`, "The monthly delta"). Once every delta is in, ERPNext's 1.1.2027 opening is bexio's
closing by construction; there is no opening import.

**The month, after each bexio month close — October, November, December 2026, then once more after the 2026
Abschlussbuchungen, when bexio's business year is closed:**

1. Benchi exports, after bexio's month close, on the Mac mini (the whole account: entities, document PDFs,
   payroll; into `private/bexio-export/<date>-complete/`, which is the `<export-dir>` below):

       BEXIO_BROKER=1 python3 finance/bexio/export.py --complete --year 2026

   (`--year 2026` keeps the payroll export on 2026 when the closing export is run in 2027; the broker-less
   fallback with the export login is in `finance/bexio/README.md`).
2. Take a backup with the files, the way `finance/docs/archiving.md` says:
   `docker compose -p frappe-finance exec -T backend bench --site frontend backup --with-files`. The
   timestamp prefix of the new set in `sites/frontend/private/backups` is the `BACKUP_ID`.
3. The yardmaster or a builder runs the dry run: `sh finance/bexio/delta.sh <export-dir>`. The counts table
   goes into the bead note.
4. ledgerdemain reads the dry run (nothing unexpected in the counts) and gives the go.
5. Apply: `DELTA_APPLY=1 BACKUP_ID=<id> sh finance/bexio/delta.sh <export-dir>` — apply pass, rerun (must
   insert nothing), trial balance check. The counts go into the note.
6. The month's trial balance per account equals bexio's; what differs are the known rows (the rappen
   roundings, the sub-centime closings on 1100, the two rows of 6641/1021 — see
   `finance/docs/erpnext-setup.md`, "Bexio history").

**December only, before the January 2027 revaluation run (erp-f5ss):** one Currency Exchange row per foreign
currency the books hold (EUR, USD, PLN, as the bank accounts show), dated 2026-12-31, with the ESTV
Jahresendkurs (public, estv.admin.ch). The revaluation job reads only those rows. Save the rates and the
source URL under `private/` first; the rows are a live write through the API user, so ledgerdemain reads the
list (currency, rate, date) and gives the go before they are written; the counts (rows written per currency)
go into the note.

**The 31.12.2026 check (January 2027):** after the closing delta, `finance/bexio/check_trial_balance.py`
compares ERPNext's 31.12.2026 balances per account with bexio's closing. Everything must be explained by the
known rows; anything else is listed privately and fixed before 1 January. The result is written into the cutover
paragraphs of `finance/docs/erpnext-setup.md` and `finance/docs/hrms.md`.

**The rules (D1/D5):** from 1.1.2027 nothing is booked in bexio; it stays read-only for Benchi and ERPNext
leads. bexio stays readable until its last filings are done from it — the 2026 Q4 MWST declaration and the
Lohnausweis 2026 — then the bexio account is cancelled and its API token revoked (Benchi).
