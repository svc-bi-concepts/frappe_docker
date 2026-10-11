# bi_payroll

Frappe app for BI Concepts' Swiss payroll (`finance/docs/hrms.md`). It is installed on the live site and the
HRMS copy (Decision 2026-10-10). It requires `hrms`, so nothing here goes into `bi_finance`, which must
migrate on any site.

- `bi_payroll/fixtures/salary_component.json`: the Salary Components with their formulas (AHV/IV/EO, ALV with
  the ceiling, BVG on a fixed yearly insured salary with the age bands, NBU, UVG, KTG, FAK), and Commission (an earning
  with no formula, paid by Additional Salary; see below). The abbreviations are the ones erpnextswiss's Salary
  Certificate reads (B, AHV, ALV, NBUV, PK). No 13th salary component: the data has none (see below).
- `bi_payroll/fixtures/custom_field.json`: the rate copies on Salary Slip (one read-only section), the Quellensteuer
  canton and code on Salary Slip (copied from the Employee on validate), and the BVG insured salary on Salary Structure
  Assignment (fixed per year).
- `bi_payroll/swiss_rates.py`: the list of rate names, shared by the settings, the slip copies and the tests.
- `bi_payroll/swiss_payroll.py`: `copy_rates_to_slip` (a `before_validate` on Salary Slip: the formulas see only the
  slip and the assignment, so each slip takes the rates from Payroll Swiss Settings when it is validated) and
  `ensure_structure` (one "Swiss Monthly" Salary Structure per Swiss company).
- `bi_payroll/bi_payroll/doctype/payroll_swiss_settings`: the settings (single). All five insurers' values stay open
  here, listed by token: the BVG age-band rates are placeholders (half the statutory minimum), UVG, NBU, KTG and FAK
  are empty (ledgerdemain, 2026-10-11: no rate is set in erp-1jh5; a follow-up bead sets them once the documents are
  provided). The KTG contract is fetched and read (erp-1jh5), its values sit in the private rates CSV.
  `finance/scripts/load-swiss-rates.sh` is ready for that step: it reads the private CSV and sets the rows marked
  set=yes — dry run on the copy by default, the live stack only with `--live --backup`.
- `bi_payroll/hand_over.py` and `bi_payroll/bi_payroll/report/payroll_hand_over`: the report `Payroll hand-over`, per
  employee and year or per employee and month, for the trustee's filing (Excel or CSV). The rows are summed from the
  submitted slips and their Salary Detail lines; the logic is pure Python, so `bi_payroll/test_hand_over.py` tests it
  with invented employees. Its view `Lohnausweis` puts the rows on the Form 11 lines per the ESTV Wegleitung 2026
  (map: `finance/docs/hrms.md`): line 1 is every earning row of the slips (Basic Salary, Commission, any Zulage),
  line 11 is line 8 less lines 9 and 10.1 — not the slips' net pay, which also deducts Quellensteuer and KTG — and
  the KTG employee share goes into line 15 as the remark "KTG-Beitrag Arbeitnehmer CHF …" (KTG has no line of its
  own). The view keeps rappen; the flag 'Whole francs' rounds the lines for the printed certificate or the export
  (round half up, `certificate_rounding`), so the printed form's arithmetic holds on its own francs.
  Two more views, for the Swiss declarations (`qst_rows` and `annual_rows` in `hand_over.py`, tests in `test_hand_over.py`):
  - `Quellensteuer Abrechnung`: per canton, per month or per quarter (filter Period), one row per employee and code with
    the slip's QST base (gross), the tax, the rate (tax over gross, the effective rate of the slips) and the canton's total.
    The slip keeps the canton and the tariff code it was computed with (`swiss_qst_canton`, `swiss_qst_code`, set in
    `withhold_qst`); only slips with a code are listed. The trustee fills the cantonal return from it and computes the
    Bezugsprovision. A slip submitted before those fields existed has no canton and is not listed. A quarterly view ignores the Month filter (it would cut the quarter).
  - `Jahresdeklarationen`: the whole year whatever the month, one row per employee and a total. Wage bases: AHV/IV/EO
    (gross), ALV (to the ceiling the slip was computed with), UVG and NBU (to the maximum), BVG insured salary and its
    coordinated part (the assignment's yearly salary, a twelfth per insured month, age 25 to 64, from the threshold on),
    FAK and KTG (gross). Then the contributions per insurance, employee and employer. For the trustee: the AHV
    Lohnbescheinigung, the UVG and KTG Lohnsummen, the BVG Lohnmeldung and the FAK declaration.
  Export as Excel or CSV, like the other views. The Quellensteuer amount is not in the Jahresdeklarationen: it has its own
  view.
- `bi_payroll/bi_payroll/structure_site_test.py`: needs a site (not a `test_*.py`, so the offline gate skips it):
  `bench --site <site> run-tests --module bi_payroll.bi_payroll.structure_site_test`. The structure rows must carry
  their components' formulas, or every slip pays 0.
- `bi_payroll/test_swiss_payroll.py`: offline tests with invented employees and rates:
  `python3 -m unittest bi_payroll.test_swiss_payroll` from this directory (no frappe needed).
- Payroll accounts (erp-nqyb): the fixture gives each Salary Component of the Company its accounts, from
  `swiss_rates.COMPONENT_ACCOUNTS` and `EMPLOYER_CONTRIBUTION_ACCOUNTS` (numbers and names only):

  | Component | Account | bexio's books |
  | --- | --- | --- |
  | Basic Salary | 5000 Löhne | debit, against 1091 |
  | Commission | 5003 Provisionen | debit, against 1091 |
  | AHV/IV/EO Employee, ALV Employee | 2271 KK AHV, IV, EO, ALV | credit |
  | BVG Employee | 2270 KK Vorsorgeeinrichtung | credit |
  | NBU Employee | 2273 KK Unfallversicherung | credit |
  | KTG Employee | 2274 KK Krankentaggeld | credit |
  | Quellensteuer Employee | 2279 KK Quellensteuer | credit |
  | AHV/IV/EO Employer, ALV Employer (Employer Contribution) | expense 5700 AHV, IV, EO, ALV; liability 2271 | debit; credit |
  | BVG Employer | expense 5720 BVG-Beitrag; liability 2270 | debit; credit |
  | UVG Employer | expense 5730 UVG-Beitrag; liability 2273 | debit; credit |
  | KTG Employer | expense 5740 KTG-Beitrag; liability 2274 | debit; credit |
  | FAK Employer | expense 5710 FAK-Beitrag; liability 2271 | debit; credit |

  The Company's default payroll payable is 1091 Lohndurchlaufkonto (`swiss-setup.sh payroll_accounts`, idempotent;
  bexio credits the net wages there and pays them from 1020). BVG Age (statistical) has none. The employer shares are
  the structure's `employer_contributions` (`STRUCTURE_EMPLOYER_CONTRIBUTIONS`), not deductions: HRMS's accrual debits the
  expense and credits the liability (decision of 2026-10-11 in erp-nqyb's note: FAK and AHV/ALV on 2271, UVG on 2273).
  The 2270 to 2279 accounts and 5720 are bexio's additions to erpnextswiss's KMU template, which has none of them. On a
  fresh site `swiss-setup.sh accounts` creates them, after `coa` and before the migrate that takes these fixtures (the
  order: coa, accounts, migrate); on the live chart they all exist, so the step skips and says so. Its table is one
  source with `test_payroll_accounts`'s `BEXIO_ADDED` (a test compares the two), which checks every number against the
  template and those additions when `BI_COA_CSV` names its `accounts_template.csv`.
- `bi_payroll/quellensteuer.py`: the ESTV withholding tariffs (Quellensteuer, Löhne, 2025 record format; source and
  version in the `QST Tariff` doctype's description). It parses a canton's text file (records 06 and 11), finds the
  bracket for a monthly gross and gives the tax (rate on the whole income, at least the minimum tax). Pure Python.
  Monthly method only: the gross of the slip, the tariff as published (it already holds the AHV/ALV/BVG share); annual,
  variable and 13th-salary cases are not handled. The 2026 file's record format is not checked yet.
- `swiss_payroll.withhold_qst` (a `validate` on Salary Slip, after HRMS has the gross): sets `swiss_qst_amount` and
  computes the deductions again; the `Quellensteuer Employee` component reads it. HRMS is not in this repo: the order
  against HRMS's own validate (the gross is known when the tax is set), the one row after the second calculation, and
  net = gross minus deductions were checked on a throwaway site with HRMS v16.50.0 (erp-5t6q, 2026-10-10, invented data).
- `bi_payroll/bi_payroll/doctype/qst_tariff`: the tariff rows (data, one per record). `swiss_payroll.load_tariff(path)`
  reloads a canton's file: the rows of its valid-from dates are replaced. Employees carry `swiss_qst_canton` and
  `swiss_qst_code` (Employee custom fields, fixtures).
- Tariff code (erp-wvbn): `swiss_qst_code` is a Link to `QST Tariff Code` (one record per ESTV code, named by the code).
  The loader makes the records of its file, and `after_migrate` (`ensure_tariff_codes`) makes those of every QST Tariff
  row, so a rerun or a migrate makes none twice. The form offers the codes of the canton (`search_qst_codes`, the Link
  query in `public/js/employee.js`); changing the canton clears a code it does not carry. On save, `validate_qst_fields`
  refuses a code without a canton and a canton/code pair that no QST Tariff row carries. A canton alone passes. The
  codes of a canton with no loaded tariff are missing: load the file first.
- `bi_payroll/test_quellensteuer.py`: offline tests with invented rows in the ESTV format:
  `python3 -m unittest bi_payroll.test_quellensteuer`.
- `bi_payroll/test_qst_codes.py`: offline tests of the code records, the form's codes and the save check, with a stand-in
  for frappe: `python3 -m unittest bi_payroll.test_qst_codes`.
- Salary Structure: `ensure_structure` makes the "Swiss Monthly" structure only when the company has none. A structure
  that no slip and no Salary Structure Assignment names (unused) and that is defective (a component missing, or a row
  whose component has a formula but which has none of its own) is cancelled if submitted, deleted and made again by
  `bench migrate`, with every row of `swiss_rates.py` and each row's formula; the Error Log gets "Swiss Salary Structure
  replaced" with the counts. A structure in use is not replaced. Its rows whose formula or condition differs from the
  component's (a fixture changed after the row was copied) are refreshed on every migrate through HRMS's own sync (the
  Desk's "sync to structures": a version row "via Salary Component sync"); the Error Log gets "Swiss Salary Structure
  rows refreshed" with the count. A refresh changes no submitted slip: slips keep the amounts they booked. A missing
  component is logged to the Error Log ("Swiss Salary Structure lacks components"), and a slip whose Quellensteuer
  amount is not zero stops on validate ("has no Quellensteuer Employee row") until the structure has it. The remake is
  for missing rows only. The manual step, for a structure in use only: make the
  structure again with every row of `swiss_rates.py` (STRUCTURE_EARNINGS, STRUCTURE_DEDUCTIONS, the Quellensteuer
  Employee last, and STRUCTURE_EMPLOYER_CONTRIBUTIONS in the `employer_contributions` table), each row with its
  component's formula (`structure_rows` does this), and submit it. Then cancel each employee's assignment on the old
  structure, make the same assignment on the new one and submit it, and cancel the old structure last: HRMS refuses
  to cancel a structure that still has submitted assignments (LinkExistsError). A Duplicate in Desk is not enough: it
  copies the old rows, and HRMS keeps a row without its formula at 0. Employees with no tariff code are not affected.
- Formulas: a structure row computes only with its formula stored (HRMS resets a row's formula on save to the value it
  had before validate). `ensure_structure` copies each component's formula onto its rows. A structure made before that
  change has empty formulas and computes 0 for every component; the live structure (erp-2s3c) was one of them.
- `bi_payroll/test_layout.py`: the layout and stamp guard, copied from `bi_finance`:
  `python3 -m unittest bi_payroll.test_layout`.
- Payroll inputs from the paystubs (`finance/bexio/import_assignments.py`, not in this app: it runs on the host through
  the API user): per Active Employee with paystubs, a submitted Salary Structure Assignment (base = the latest
  month's salary line, 1001 up to 2025 and 1000 from 2025, from its first day); the Employee's `swiss_qst_canton` (the
  bexio address canton) and `swiss_qst_code` (the one QST Tariff code of the canton that holds the gross's bracket at
  the printed 5060 rate; several candidates are reported, not guessed). An employee whose latest paystub prints no
  5060 has QST ended: no canton, no code (the apply clears a canton set earlier); the empty settings' employee rates
  (5010, 5020, 5040), filled only when the Active employees' latest paystubs agree on one rate. The BVG insured salary is a
  calibration: inverted from the 5050 through the band rate by the BVG Employee formula
  (`coordination + 1200 * amount / rate`, to the franc), so the formula reproduces the latest paystub until the fund's
  sheet replaces the band rates and the insured salaries. Each one is listed in `private/payroll/bvg-calibration.txt`.
  `python3 -m unittest test_import_assignments` in `finance/bexio`.
- `finance/images/bi_payroll.Containerfile`: the layer on the image with bi_finance (live and copy), built by
  `finance/scripts/build-image.sh`.
- Commission (HR 3, wage type 1218): an earning with the abbreviation COMM, no formula and amount 0 in the structure, so
  the structure names it. It is paid per month through Additional Salary, and it is in the slip's gross: AHV/IV/EO,
  ALV (under the monthly ceiling), NBU, UVG, KTG, FAK and the Quellensteuer (`withhold_qst` reads the gross) take it in.
  BVG does not: its base is the assignment's insured salary, fixed per year, so a commission leaves BVG as it was. Whether
  the insured salary follows the salary is open (`finance/docs/hrms.md`, question 3). The account is 5000 (Löhne); 5003
  (Provisionen) is on bexio's chart, and the component takes it only if the GL shows commissions there (none does yet).
  Like the other components, it has no GL account on the fixture.
- Additional Salary, for Benchi (Desk: HR > Additional Salary > New): one per employee and month for a commission. The
  fields are the employee, the salary component "Commission", the amount, and the payroll date (the month the
  commission belongs to). The Payroll Entry for that month picks it up, and the slip computes AHV, ALV and the rest
  on salary plus commission. Not yet run on a site: HR 6 (the copy) checks the slip before the first live run.
- The live structure is in use, so the migrate does not add the Commission row to it: `ensure_structure` logs "Swiss Salary
  Structure lacks components" to the Error Log. The commission then still pays through Additional Salary (HRMS adds an
  Additional Salary component to the slip even when the structure does not hold it, to check on the copy), but the
  structure is made again by hand (the Salary Structure bullet above) before the first live run.
- Wage types (bexio paystubs, public numbers and names only) and the component each one feeds:

  | Wage type | Name on the paystub | Component (fixture) |
  | --- | --- | --- |
  | 1000 | Monthly salary | Basic Salary |
  | 1001 | Monthly salary 2 | Basic Salary (a second monthly salary line; the same component) |
  | 1005 | Hourly wage | none (no hourly employment in the data since 2024) |
  | 1160 | Vacation compensation | none (not in payroll yet) |
  | 1218 | Commission | Commission |
  | 5000 | Gross wages | the gross (`gross_pay`), not a component |
  | 5010 | AHV/IV/EO contribution | AHV/IV/EO Employee |
  | 5020 | ALV contribution | ALV Employee |
  | 5040 | NBU contribution | NBU Employee |
  | 5050 | BVG contribution | BVG Employee |
  | 5060 | Withholding tax | Quellensteuer Employee |
  | 5900 | Total deductions | the sum of the deductions |
  | 6500 | Net salary | the net (`net_pay`) |
  | 6510 | Advance payment | none (a payment, not payroll) |
  | 6600 | Amount paid out | none (a payment, not payroll) |
  | 6030, 6070, 6300.1 | Other effective expenses, other lump sum expenses, Rent | none (expenses; HR 11 covers the route) |

  The employer shares are not on the paystub; the fixture's employer components carry them.
- 13th salary (HR 3, from the figures, counts only): no 13th salary, bonus or double December in the data. Of 777 lines
  for 9 employees over 35 months, no wage type is a 13th salary or a bonus; 1005 is the hourly wage (3 lines, one
  employee, 2023 and 2024). In no December is the salary more than 1.5 times the employee's median monthly salary, and no
  December month holds both a 1000 and a 1001 line. So the component needed is none. Add a "13th Salary"
  component, paid in December through Additional Salary, only when a contract says so.
- Commission in the data (counts only): 6 lines of wage type 1218 for 2 employees over the 35 months.
