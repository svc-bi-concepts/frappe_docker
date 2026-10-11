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
