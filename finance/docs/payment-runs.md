# Payment runs (pain.001 for UBS)

A payment run pays due purchase invoices from the UBS current account. ERPNext
makes the Payment Proposal and the pain.001 file (erpnextswiss); the app
`bi_finance` adds the Payment Entries and the Swiss schema check
(`finance/apps/bi_finance/bi_finance/payment_run.py`). Benchi uploads the file in
UBS e-banking; the camt import later matches the bank lines to the Payment Entries.

ERPNext does not send payments to a bank.

Until 2027-01-01, bexio prepares supplier payments; Benchi releases them in the bank.

## Set up (once)

1. The Bank Account "UBS Kontokorrent - UBS Switzerland AG" holds the IBAN. The
   step below copies it to the GL Account "UBS Kontokorrent" (erpnextswiss reads it
   there), and sets the BIC of UBS Switzerland AG (UBSWCHZH80A, public) on the Bank
   and the GL Account when they are empty. It does the same for every other company
   bank account with an IBAN (the Wise account today). Without an IBAN and a BIC, no file can be made.
2. `finance/scripts/swiss-setup.sh payments`. It sets the erpnextswiss settings the
   run needs (pain.001.001.09, region CH, unidecode on), makes the IBAN and BIC fixes
   above, and says if the account still lacks an IBAN or BIC. A second run changes nothing.
3. `finance/scripts/swiss-setup.sh expenses` (once, with `HR_USER` set). It creates the
   Expense Claim Types the claim route books on (`finance/docs/operating.md`, Expenses) and
   the approver role; without it the run finds no claims and a claim without a payable
   account cannot be submitted.

## Run

1. Payment Proposal list, the create button. It takes the open Purchase Invoices due
   by the planning date (`planning_days` in ERPNextSwiss Settings), the submitted
   expense claims with status Unpaid of the company (HRMS), and the pay-from account.
   Check the list and the total. A claim row holds what the employee is still owed
   (grand total: the sanctioned amount plus the VAT rows, minus advances).
2. Submit. Each paid invoice gets a Payment Entry as a **draft** (pay from the UBS
   account, to the creditor account of the invoice, allocated to that invoice), and
   each claim row one to the claim's payable account (2003 Verbindlichkeiten Spesen).
   Invoices are then marked as proposed, so a second run does not pick them up; the
   same for claims.
3. "Download bank file" on the submitted proposal. The file must pass the Swiss
   schema check or nothing is downloaded. On success the draft Payment Entries are
   **submitted**, dated the day of the download at the latest.
4. Upload the file in UBS e-banking and approve it there.

Cancelling the proposal cancels its submitted Payment Entries and deletes the
drafts, and un-marks the invoices and the claims.

## The file

- Version: pain.001.001.09, the ISO 20022 version of the Swiss Payment Standards.
  erpnextswiss validates against the generic schema of that version; this app also
  validates against SIX's Swiss variant (`pain.001.001.09.ch.03.xsd`), which is the
  one banks name in their documentation.
- EndToEndId is what erpnextswiss writes: the supplier's invoice number (`bill_no`, else
  the Purchase Invoice name), cut to 35 characters; the invoices of one supplier in a run
  are one file row, so their numbers are joined by a space. An expense-claim row is the
  claim's name, joined the same way for one employee. It is **not** the Payment Entry
  name. Each Payment Entry carries the same number as its `reference_no`, so the camt
  import (erp-f5ss) matches a bank line to Payment Entries on that number and the amount.
- The company's country code is written upper case (erpnextswiss writes it lower case).
- An IBAN whose check digits fail (mod 97) stops the file before it is made, naming the
  row; the bank would refuse the file otherwise. An empty IBAN is not checked here.
- **Not verified:** that UBS e-banking accepts this file. No UBS source found
  confirms the version or the Swiss variant. Acceptance is manual, on the first real
  run: upload the file in UBS e-banking, check each payment (creditor, IBAN, amount,
  date) before release; nothing is paid until it is released. A rejected file: copy
  UBS's message into a note on the bead.

## Limits

- One Payment Entry per invoice. An aggregated transfer (several invoices to one
  supplier) is one bank line, so the camt match is one line to several Payment
  Entries; check that ERPNext's bank reconciliation takes it.
- Invoices in a currency other than the pay-from account's are refused (a Payment
  Entry needs an exchange rate, not built yet).
- Salaries are left out of the Payment Entries of this app: the net pay is posted by the
  Payroll Entry's bank entry (HRMS); the proposal only carries the bank instruction.
- Expense-claim rows take a Payment Entry like a bill (pay from the UBS account, to the
  claim's payable account), so the camt line of the reimbursement reconciles the same way.
  The employee needs a bank account number and a two-line address on the Employee record,
  or the proposal refuses (`bank_ac_no`, `permanent_address`).
- The Payment Entry is dated on the download day, not on the execution date.

## Tests

Offline, invented data, in the image (no site, no database):

```sh
docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/frappe-bench/apps/bi_finance:ro" \
    frappe-finance-custom:v16.50.0-swiss-bi9 \
    sh -c 'mkdir -p /home/frappe/logs && cd /home/frappe/frappe-bench/apps/bi_finance && ../../env/bin/python -m unittest -v bi_finance.test_payment_run'
```

They cover the amount rule (skonto), the Payment Entries for invoices and for expense-claim
rows, the currency refusal, the submit on download, the cancel, the create button without
HRMS, and a rendered file that passes both schemas.

The IBAN and BIC rule of the `payments` step is tested offline with the other
swiss-setup tests: `python3 -m unittest discover -s finance/scripts -p 'test_*.py'`.
