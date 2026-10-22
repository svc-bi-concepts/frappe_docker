# Swiss payroll on ERPNext v16: HRMS fit study

Decision document for running Swiss payroll in ERPNext v16 (ERPNext `v16.50.0`),
the way `swiss.md` decides the Swiss accounting. Benchi decided on 2026-10-10: full
HRMS in ERPNext, with the payroll history from bexio included. This document says how,
and what is still open. HRMS is in `finance/apps.json` (live) and `finance/apps-copy.json` (the copy
site, see [Copy stack for the HR train](#copy-stack-for-the-hr-train)). Payroll runs on the live site too (Decision 2026-10-10, below):
the live stack runs the HRMS image named in `finance-local.yml` (bi11), with `hrms` and `bi_payroll`
installed on `frontend`.

Public facts only: no company data, no employee data, no amounts from the company.
Those stay in `private/`.

## Verdict

**Fit for the payroll core; four Swiss gaps, two of them need a decision before go-live.**

- Frappe HRMS `v16.50.0` gives what the payroll needs underneath: Salary Components with
  formulas, Salary Structures, Salary Slips, Payroll Entry for accruals and bank entries,
  and the leave and expense-claim doctypes.
- erpnextswiss `v16` (already on the site) has a Lohnausweis doctype (Salary Certificate),
  a salary payment step for the pain.001 run, and the AHV number on Employee. These need
  HRMS to work: they read `Salary Slip` and `Salary Detail`.
- HRMS has no Swiss rules. AHV/IV/EO, ALV, BVG, UVG/NBU, KTG and FAK can be written as
  Salary Components with formulas, with no code. Quellensteuer and ELM are not covered by
  either app (see Gaps).

## Decision (2026-10-10)

Benchi decided on 2026-10-10:

- **Calculator:** ERPNext with HRMS, as above. Live payroll in ERPNext as soon as the HRMS
  copy passes, still in 2026.
- **ELM:** no own Swissdec certification. The transmission is bought from a certified vendor,
  if one takes our data (see `elm.md`). Vendor inquiry: not now.
- **2026 wage reports:** done by the company's trustee / payroll service. The payroll
  hand-over export is their input: the report `Payroll hand-over` in `bi_payroll`, per employee
  and year, or per employee and month. It lists gross, each Swiss deduction by component
  (AHV/IV/EO, ALV, BVG, UVG/NBU, KTG, FAK, Quellensteuer), net, the employer shares and the
  AHV number, and exports as Excel or CSV. It runs on the live site and on the HRMS copy.
  Its roles include System Manager, so the yard's API user runs it through the API (erp-tatn).
  For the declarations it has two more views (`bi_payroll/README.md` says what each is for): `Quellensteuer Abrechnung`,
  the withholding per canton and per month or quarter for the cantonal return, and `Jahresdeklarationen`, the year's
  wage sums per insurance (AHV/ALV, UVG and NBU, BVG, FAK, KTG) with the contributions, per employee and in total.

**Decision 2026-10-10: payroll runs on the live site.** The live ERPNext is a test environment,
so HRMS `v16.50.0` and `bi_payroll` are installed on the live site `frontend`, not only the copy.
This replaces the rule "live = base + bi_finance, HRMS on the copy only". `apps.json` lists hrms,
and `build-image.sh` builds `bi_payroll` on top of `bi_finance`. The copy stack stays as a sandbox.

**Open:** certified ELM transmitter: inquiry later.

The hand-over rows are checked against invented employees (`python3 -m unittest
bi_payroll.test_hand_over`) and were run on a throwaway site with invented slips (erp-fs9c: rows
equal the slips per year and per month). Two parts are open and stay so: Quellensteuer (in payroll since erp-6rrh,
a component set from the gross) was not yet on the site the proof ran on, so its column there is 0, and the
Lohnausweis line mapping (below) is not checked against the 2026 form on the copy yet (step 6);
lines 1, 11 and 15 follow the Wegleitung text since erp-4yyn.

## Versions

| Item          | Pin                                                                     | Evidence                                          |
| ------------- | ----------------------------------------------------------------------- | ------------------------------------------------- |
| ERPNext       | `v16.50.0`, commit `7474d9e` (live)                                     | tag, as in `apps.json` and `swiss.md`             |
| Frappe        | `v16.50.0`, commit `f20f92d`                                            | as in `swiss.md`                                  |
| HRMS          | tag `v16.50.0`, commit `7c03769` (branch `version-16`, head 2026-10-07) | `gh api repos/frappe/hrms/git/refs/tags/v16.50.0` |
| HRMS requires | `frappe/erpnext` (`required_apps` in `hrms/hooks.py`); Python `>=3.10`  | `pyproject.toml` on `version-16`                  |

`v16.50.0` is the HRMS tag that matches ERPNext `v16.50.0`. Pin it the way `apps.json`
pins erpnext: a tag, with the commit noted here. The branch `version-16` moves; the tag does not.

## What HRMS covers (v16)

Source: `hrms/hr/doctype` (119 doctypes) and `hrms/payroll/doctype` on `version-16`.

- **Payroll:** `salary_component`, `salary_structure`, `salary_structure_assignment`,
  `salary_slip` (with `salary_detail` rows: `abbr`, `amount`, `year_to_date`),
  `payroll_entry`, `payroll_period`, `additional_salary`, `employee_other_income`,
  `arrear`, `retention_bonus`, `gratuity`, `payroll_correction`, `salary_withholding`.
- **Leave and time:** `leave_application`, `leave_allocation`, `leave_policy`,
  `leave_ledger_entry`, `attendance`, `shift_*`.
- **Expenses:** `expense_claim` and its details and taxes (the table that `payment-runs.md`
  says the site lacks until HRMS is in).
- **Accounting:** a Salary Slip submit makes no GL entry (`salary_slip.py`, `on_submit`).
  The GL comes from `Payroll Entry`: the accrual Journal Entry (`make_accrual_jv_entry`),
  which takes only the submitted slips of that Payroll Entry (`payroll_entry.py`,
  `get_sal_slip_list`, lines 311 to 327: `payroll_entry` set to it, and an empty
  `journal_entry`), and the bank entry for the net pay (`make_bank_entry`).
- **Employee:** not in HRMS. It is in ERPNext core (`erpnext/setup/doctype/employee`).

Regional code: `hrms/regional` has `india` and `united_arab_emirates` only. There is no
Swiss module.

## What HRMS lacks for Switzerland

- No AHV, ALV, BVG, UVG, KTG or FAK components. Their rates and ceilings are not in the app.
- No Quellensteuer tariff. Its `income_tax_slab` is a generic progressive table, not the
  cantonal withholding tariffs.
- No Lohnausweis (Form 11) and no ELM transmission.
- No pain.001 for salaries. Payroll Entry makes Journal and Bank Entries; the bank file is
  erpnextswiss's (see the pay step below).

## Swiss needs and how each is met

Rates are the 2026 public values from the sources in the last column. They are inputs to
the Salary Components, not hard-coded in the app. Each must be checked against the
insurer's or the Ausgleichskasse's own sheet before a live run.

| Need                                         | How it is met                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 2026 public values                                                                                                                                                                                           | Status                                                                                                                                                                                                         | Source                                                                                                                                                                                                                                                                        |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AHV/IV/EO                                    | Salary Components with a formula on the gross AHV wage: 5.30% employee, 5.30% employer                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 10.60% total, 5.30% each side. The sources do not agree on the split between AHV, IV and EO; the formula does not need it                                                                                    | Config, no code. Check the wage base (family allowances excluded) with the Ausgleichskasse                                                                                                                     | [weka](https://www.weka.ch/themen/personal/lohn-und-gehalt/lohnabrechnung/article/lohnabzuege-die-aktuellen-sozialversicherungsbeitraege/), [onlinetreuhand 2026](https://www.onlinetreuhand.ch/fileadmin/user_upload/Onlinetreuhand/Dokumente/Sozialversicherungen_2026.pdf) |
| ALV (incl. ceiling)                          | Component with `min(base, ceiling)`: 1.10% employee, 1.10% employer                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 2.20% total up to CHF 148,200 a year (unchanged from 2025). No contribution above the ceiling: the 1% solidarity contribution (ALV 2) lapsed on 1 Jan 2023                                                   | Config, no code. Nothing above the ceiling                                                                                                                                                                     | same sources; [law.ch](https://law.ch/lawnews/2022/10/arbeitslosenversicherung-solidaritaetsprozent-entfaellt-per-01-01-2023/)                                                                                                                                                |
| BVG (coordinated salary)                     | Component on `salary - coordination deduction`, with an entry threshold, and the age-based rates of the Pensionskasse. The split and the rates are the fund's plan, not the law                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Entry threshold CHF 22,680; coordination deduction CHF 26,460                                                                                                                                                | Config is possible (the formula reads the employee's date of birth). The plan rates are open                                                                                                                   | [convit](https://convit.ch/wissen/bvg-koordinationsabzug), [auditrium](https://www.auditrium.ch/blog/articles/koordinationsabzug-2026-wichtige-aenderungen-fuer-unternehmen)                                                                                                  |
| UVG / NBU                                    | Components with the insurer's percentage on the insured salary. BU/UVG employer share, NBU employee share                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Set by the insurer per policy                                                                                                                                                                                | Config. Needs the policy sheets                                                                                                                                                                                | none; insurer documents                                                                                                                                                                                                                                                       |
| KTG                                          | Component with the insurer's rate on the insured salary                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Set by the insurer                                                                                                                                                                                           | Config. The 2026 KTG contract is read and its values are set in the settings (2026-10-11), the employee side checked against the paystubs; the employer pays the premium, so the sheet names no employee share | the contract in `private/payroll-insurance/`                                                                                                                                                                                                                                  |
| FAK                                          | Employer component on the AHV wage, rate by canton and Ausgleichskasse                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Set by the fund                                                                                                                                                                                              | Config. Needs the fund's rate                                                                                                                                                                                  | none                                                                                                                                                                                                                                                                          |
| Quellensteuer                                | Not covered. erpnextswiss's Salary Certificate settings have a field "12 Quellensteuerabzug" that maps a component; it does not compute the tax                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Cantonal tariffs, per year                                                                                                                                                                                   | **Gap.** Build (bi_finance) or outsource. See Questions                                                                                                                                                        | `erpnextswiss` `salary_certificate_settings.json`                                                                                                                                                                                                                             |
| 13th salary                                  | A Salary Component paid in December, or an accrual over the year (`accrual_component`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Contract                                                                                                                                                                                                     | None in the data (Question 5). No component until a contract says so                                                                                                                                           | HRMS `salary_component` fields                                                                                                                                                                                                                                                |
| Lohnausweis (Form 11), per employee per year | erpnextswiss `Salary Certificate`: sums `Salary Slip` gross and net over the year, and the `Salary Detail` rows by component name (the names in its tables, e.g. Basic Salary, AHV/IV/EO Employee) into the form's fields. Fields are set in `Salary Certificate Settings` (dynamic configuration, step `lohnausweis`; see "Lohnausweis map" for why by name, not abbr). Its Nettolohn (Ziffer 11) and its component SQL are corrected by our patches (`finance/patches/erpnextswiss` 0002-0004)                                                                                                                                    | ESTV Wegleitung for Form 11 applies from 1 Jan 2026                                                                                                                                                          | Needs HRMS. The field layout is checked line by line against the 2026 Wegleitung in "Lohnausweis map"; lines 1, 11 and 15 follow the Wegleitung since erp-4yyn (the copy proof stays step 6)                   | [ESTV Wegleitung 2026](https://www.estv.admin.ch/dam/de/sd-web/afP1GDFr8gE3/dbst-form-lohna-wegleitung-2026-de.pdf); the PDF is read (Ziffer 1, 10.1, 11, 15), `private/payroll/forms/wegleitung-lohnausweis-2026.pdf`                                                        |
| ELM / Swissdec transmission                  | Not in erpnextswiss `v16` (no ELM file in its tree), not in HRMS                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | ELM 4.0 switched off; ELM 5.0 required for the 2026 payroll year. Sources disagree on the 4.0 end date (30.06.2026 per BFS; end 2025 per one vendor). Transmission needs Swissdec-certified payroll software | **Gap.** See Questions and `elm.md`                                                                                                                                                                            | [BFS](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html), [Swissdec](https://www.swissdec.ch/abschaltung-elm-4-0), [AXA](https://www.axa.ch/de/unternehmenskunden/melden-und-mutieren/melden/elektronische-lohnmeldung.html)                                           |
| Payslip layout                               | bi_payroll's `Lohnabrechnung` print format on Salary Slip (erp-h47v): Jinja template `lohnabrechnung.html`, German, generic (company and employee from the records at print time); the rows, bases and rates are prepared by `bi_payroll/payslip.py` (jinja method `payslip_data`) from the slip's own tables, with the employer shares in a separate block, year-to-date gross and net, and the masked AHV number and bank account. Installed and made the Salary Slip default by `swiss-setup.py` step `payslip`. erpnextswiss ships `kt_salary_slip.html`, a print for one of its own sample companies; used as a reference only | none                                                                                                                                                                                                         | Done in the HR train; installed live 2026-10-11 (erp-zg4p) and made the Salary Slip default. PDF proof on the HRMS copy (erp-rqxs); a payslip PDF printed on live itself is still open                         | erpnextswiss `templates/print_formats/kt_salary_slip.html`; `finance/apps/bi_payroll/bi_payroll/lohnabrechnung.html`                                                                                                                                                          |
| Net salary payment (pain.001)                | erpnextswiss `Payment Proposal` with `Payment Proposal Salary Slip` (the salary side of the payment run). The run in `payment-runs.md` leaves salaries out while the site has no HRMS                                                                                                                                                                                                                                                                                                                                                                                                                                               | pain.001.001.09 as in `payment-runs.md`                                                                                                                                                                      | Test on a copy after HRMS (erp-xpmr)                                                                                                                                                                           | `payment-runs.md`; erpnextswiss `payment_proposal_salary_slip`                                                                                                                                                                                                                |
| Employee AHV number                          | erpnextswiss custom fields `social_security_number` and `old_social_security_number` on Employee                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | n/a                                                                                                                                                                                                          | Present in `v16` (`custom/employee.json`)                                                                                                                                                                      | erpnextswiss `v16`                                                                                                                                                                                                                                                            |

## Roles and settings (erp-mvr9)

What HRMS v16.50.0 installs and what the `hr` setup step makes of it
(`erpnext-setup.md`, The setup script):

- **Roles.** The role profile "HR" holds HR User, HR Manager, Leave Approver and
  Expense Approver (`hrms/setup.py`, `DEFAULT_ROLE_PROFILES`); the install also
  creates the custom role "Employee Self Service", which gates the self-service
  portal (User Type with `apply_user_permission_on` Employee). There is **no
  Payroll Manager role in HRMS v16.50.0** (none in the whole tree at the tag):
  the payroll doctypes — Salary Slip, Salary Structure, Payroll Entry — are the
  HR Manager's (`payroll_entry.json`: HR Manager only). Benchi's user therefore
  gets HR Manager, HR User and Leave Approver; Payroll Manager is granted by the
  step only when the site has the Role document, never created. The API user
  keeps System Manager and its HR User, and gets nothing more (erp-wnx7).
- **Payroll Settings.** `payroll_based_on` = Leave (the default),
  `include_holidays_in_total_working_days` = 1, `email_salary_slip_to_employee`
  = 0. That last check is the only slip e-mail switch: `salary_slip.py`
  `on_submit` reads exactly it, so off means no automatic slip e-mail on submit
  and none by hand. There is **no rounding setting** in v16.50.0; CHF rounds to
  0.05 through the Currency smallest fraction (`currencies` step).
- **HR Settings.** v16.50.0 has **no switch** for employee self-service and none
  for onboarding e-mails, and the step invents none. Self-service is off when no
  enabled user holds the "Employee Self Service" role and no Employee carries a
  user (`user_id`); onboarding e-mails go only from Employee Onboarding
  documents, of which the site has none — the importers write Employees
  directly. `hr --check` prints the counts (enabled users with HR or payroll
  roles, employees with a user, Employee Onboarding documents) and, with
  `HR_USER` set, compares the wanted state, exit 1 on a difference.
- **No live write in this bead** (train rule): the step and its check were proven
  on the HRMS copy; the live run is the go-live bead's and is done (erp-zg4p,
  2026-10-11): `HR_USER` read from User (the one enabled human System User, never
  written here), 1 role added, 2 Payroll Settings values changed, rerun 0, and
  `HR_USER=<user> sh finance/scripts/swiss-setup.sh hr --check` exits 0.
- **Where Swiss payroll lives in the Desk (erp-3ce8).** HRMS's Payroll workspace is the Home of the **Swiss Payroll**
  shell, which bi_payroll ships (its Sidebar, `bi_payroll/bi_payroll/sidebar/`) and mounts on ERPNext's rail (its Dock
  mounts on `erpnext`). Payroll Entry and Salary Slip sit there as the daily documents; Payroll Swiss Settings, QST
  Tariff Code and QST Tariff under Setup, and Payroll Hand-over under Reports, are bi_payroll's and claimed by that
  shell: a bare route to one of those opens here. Payroll Entry and Salary Slip, reached bare, still open in HRMS's own
  Payroll shell. The Treasury page no longer carries a payroll card.

## Migration from bexio

The bexio payroll export (bead erp-5sbz) is not run yet, so this section maps the entities
the bead asks for. Counts come with that export. Nothing below has been read from `private/`.

| bexio payroll                             | ERPNext / HRMS doctype                                                                         | Note                                                                                                                         |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Employees                                 | Employee (ERPNext core)                                                                        | AHV number to `social_security_number`. Pay data is not on Employee                                                          |
| Salary types and insurance settings       | Salary Component, Salary Structure                                                             | Set `abbr` to the codes erpnextswiss reads: B, AHV, ALV, NBUV, PK. Without these codes the Lohnausweis sums come out as zero |
| Paystubs (PDF, per employee, year, month) | Private File on the Employee (`import_paystubs.py`), no Salary Slip: the export has no figures | Reference only; the Lohnausweis of past years stays bexio's (step 5)                                                         |
| Absences per employee and business year   | Leave Application (or a Leave Allocation balance)                                              | Only for the open year, or as a balance. Per-day history is not needed for the Lohnausweis                                   |
| Payroll journal lines (booked)            | Journal Entry (already loaded by `import_payroll.py`, bexio_id `journal-<id>`)                 | Stays as the GL                                                                                                              |

The figures of the exported paystub PDFs (monthly salary, BVG, the Quellensteuer base and rate, the
AHV/ALV/NBU rates) are parsed into one private table by `finance/bexio/parse_paystubs.py`, so the
per-employee setup reads them without opening the PDFs again.

### History: Salary Slips, not new GL

The GL already holds the history: `import_payroll.py` loads the payroll journal lines as
Journal Entries. Creating Salary Slips for those months does not create GL again, if the
slips are never picked up by a Payroll Entry accrual. The accrual picks only slips that
name that Payroll Entry (`payroll_entry`) and have an empty `journal_entry`
(`payroll_entry.py`, `get_sal_slip_list`). The rule for history: leave `payroll_entry`
empty. That alone excludes them. Setting `journal_entry` as well is optional: it is one Link,
and `import_payroll.py` loads one Journal Entry per bexio journal line (`journal-<id>`), so a
month can have several. Link it only when a month has a single entry; otherwise leave it empty. This is the design to test. It is not yet run.

The check after the load is the one `import_payroll.py --check` already does: the GL per
account and year against bexio's lines, with counts and totals only in the notes.

### Cutover 2027

Decision D1/D2 (2026-10-11): bexio runs the 2026 payroll to the end; ERPNext's first payroll is January 2027
(HR 7). The GL the slips start from stays level with bexio through the monthly delta
(`finance/bexio/delta.sh`, `finance/docs/operating.md`, "Cutover"): the payroll journal lines of each bexio
month arrive as Journal Entries keyed `journal-<id>`, and the 31.12.2026 check (after the closing delta)
confirms ERPNext's balances equal bexio's closing per account — result: (pending, January 2027, written into
`finance/docs/erpnext-setup.md`, "Cutover 2027"). From 1.1.2027 the payroll books only in ERPNext; bexio
stays read-only until its Lohnausweis 2026 is done, then is cancelled.

## Plan

Ordered beads. Each is one change, offline first; the live site is only changed in a deploy
slot that ledgerdemain gives (`erpnext-setup.md`).

1. **Benchi's decisions** (the Questions below). No code.
2. **HRMS image on a copy.** Add `hrms` (tag `v16.50.0`) to `finance/apps-copy.json` (the live
   list stays without it) and build the copy image as `swiss.md` does. Install on a copy of the site after a backup. Check that
   erpnextswiss and bi_finance still migrate and their tests pass.
3. **Swiss salary components and structure.** Components with the formulas above, as
   fixtures of the new app `bi_payroll` (`finance/apps/bi_payroll/`), not `bi_finance`: a fixture that
   names an HRMS doctype would break `bi_finance`'s migrate on a site without HRMS. The app
   requires `hrms` and is installed on the live site and the copy (Decision 2026-10-10). The rates are the doctype Payroll Swiss
   Settings, not constants in code, so a 2027 change is a settings edit; each Salary Slip keeps
   the rates it was computed with. Invented-data tests for each formula, with the ALV ceiling and
   the BVG threshold (`python3 -m unittest bi_payroll.test_swiss_payroll` in the app folder).
   Payroll accounts (erp-nqyb): the Salary Components carry their Company's accounts from bexio's chart
   (`swiss_rates.COMPONENT_ACCOUNTS` and `EMPLOYER_CONTRIBUTION_ACCOUNTS`, the table in the app's README), and
   `swiss-setup.sh payroll_accounts` sets the Company's default payroll payable to 1091 Lohndurchlaufkonto. The six
   employer shares are Employer Contribution components in the structure's `employer_contributions` (decided
   2026-10-11), so the accrual books each as expense 57xx against a 227x liability. Live steps, all in the go-live
   bead after the train lands (erp-nqyb's done note): migrate with the fixtures; `swiss-setup.sh payroll_accounts`;
   remake the live "Swiss Monthly" structure with `employer_contributions` (a new version, new assignments for the
   Active employees from the existing ones, the old structure left unused). The accrual JE is proven on the copy
   before go-live. **Live 2026-10-11 (erp-zg4p):** done, as one idempotent script
   (`finance/scripts/swiss-structure-remake.sh`, commit 4c6057d): the default payroll payable set to 1091 first,
   then "Swiss Monthly BI Concepts 2" submitted with the 2 earnings, 7 deductions and all 6 employer
   contributions, the 3 assignments moved (same base, same insured salary; the payable filled by validate), the
   old structure cancelled last. Read-backs green: 0 submitted assignment with a null payroll_payable_account,
   and a Payroll Entry preview finds every Active employee.
4. **Employee load from the bexio export.** Offline importer, the same shape as
   `import_master.py`, writing Employee fields including the AHV number and BI Payroll's
   employment level, weekly hours and stay permit fields. Mocked data in tests.
   Done 2026-10-11 (erp-y909): `swiss-setup.sh fields` (Employee in `BEXIO_DOCTYPES`),
   `create-api-user.sh` (the API user's HR User role), backup `20261011_001457`, then
   `python3 finance/bexio/import_employees.py --dry-run` and `--apply`: 9 Employees on live
   (3 Active, 6 Left), rerun 0 new, nobody without a joining date (`private/payroll/needs-dates.txt`
   says 0). Counts only here; the importer prints counts and changed field names by row position.
   The go-live rerun (erp-zg4p, 2026-10-11) filled the three payroll fields (employment level, weekly hours,
   stay permit): 9 updated, 0 created, 0 failed, rerun 0.
   4b. **Payroll inputs from the paystubs** (erp-y4yf, `finance/bexio/import_assignments.py`). Derived from the
   parsed figures (`parse_paystubs.py`): one submitted Salary Structure Assignment per Active employee on
   the "Swiss Monthly" structure, base = the monthly salary (1001 up to 2025, 1000 from 2025) of the latest
   paystub month, from its first day; the Quellensteuer canton (the bexio address, where a 5060 line exists) and
   tariff code (the QST Tariff row of the canton that holds the gross's bracket at the printed 5060 rate, unique
   only). An employee whose latest paystub prints no 5060 has QST ended: no canton, no code (the apply clears a
   canton written earlier); the employee rates 5010, 5020 and 5040 in Payroll Swiss Settings, when empty and when the Active
   employees' latest paystubs agree. Left employees get no assignment (their figures stay for the history).
   **The BVG insured salary is a calibration, not the fund's figure** (ledgerdemain, 2026-10-11): the band rates
   are placeholders, so the insured salary is worked back from the paystub's 5050 through them, to reproduce
   that amount in the BVG Employee formula. Without it the first payroll would deduct no BVG. Each calibrated
   value is listed in `private/payroll/bvg-calibration.txt`; both are replaced when the fund's plan sheet arrives.
   The insured salary on the assignment is a YEARLY amount (the formula divides it by 12): a monthly-scale value
   computes a zero BVG deduction, and the BVG row then drops off the payslip and off line 10.1 of the Form 11
   (found by the erp-rqxs PDF proof on the copy).
   **Stays manual:** the employer rates (AHV, ALV, UVG, KTG, FAK: the insurers' sheets; KTG is printed on no paystub),
   and the BVG plan (the band rates in the settings, then the insured salaries with them). Rates state (2026-10-11):
   KTG's two fields (swiss_ktg_ee, swiss_ktg_ag) are set, from the insurer's contract, the employee side checked
   against the paystubs; swiss_uvg_ag, swiss_uvg_max, swiss_nbu_ee and swiss_fak_ag stay placeholders, the documents
   still awaited; the BVG bands stay calibrated from the paystubs. The QST
   Tariff rows are loaded by `bi_payroll.swiss_payroll.load_tariff` from the ESTV file before the codes resolve; the
   Employee's code is picked from the QST Tariff Code list of its canton (erp-wvbn), and the loader makes those codes.
   Live (erp-zg4p, 2026-10-11): the assignments sit on the new structure (moved by the remake above); a rerun of
   the importer plans nothing (3 already made, 0 settings to fill, 4 Employee fields unchanged).
   4c. **Leave and holiday setup** (`finance/bexio/import_leave.py`): the 2026 canton-wide SZ public holidays
   from the Kanton Schwyz list, Ferien, the 2026 Leave Period and one submitted allocation per Active Employee.
   HRMS v16 resolves an Employee's holidays through a submitted Holiday List Assignment, no longer through
   `Employee.holiday_list`, so the importer upserts one assignment per Active Employee (applicable_for Employee,
   the 2026 list, from 1 January) next to the still-existing field and keeps the Company default. An entitlement
   of 0 days gets no allocation (HRMS refuses 0); the run counts it as `no entitlement` (no failure) for Benchi to
   check. Missing annual days use the OR minimum (20, or 25 under age 20), pro-rated by employment level; the private
   `leave-defaults.txt` list is for Benchi to check. Benchi's explicit entitlements are in the private
   `leave-overrides.txt` (bexio id and days, tab separated, mode 600): an override wins over the export and over the
   default, 0 means no allocation, and an overridden employee is not listed in `leave-defaults.txt`; a malformed row
   stops an apply before any write. Absence history stays out because bexio's export is empty.
   Dry run and repeatability are covered by invented-data tests. The train rule reserves all setup and live writes
   for the HR go-live bead; no live write in this child.
   Live (erp-zg4p, 2026-10-11; the yardmaster ran the `--apply`, the builder's write was denied by the auto mode):
   created 6, updated 4, failed 0, one Holiday List Assignment per Active Employee (3). The 2 default allocations
   of `leave-defaults.txt` (Benchi's H3: 0 days for those 2) were cancelled afterwards, kept as cancelled for the
   audit trail, so live holds 1 submitted allocation. A rerun plans those 2 defaults again: do not rerun until
   erp-tn1a (an explicit 0 in an overrides list wins) has landed.
5. **Payslip history as reference** (erp-2c3a, `finance/bexio/import_paystubs.py`). Replaces the
   earlier plan of one submitted Salary Slip per employee and month (Decision, ledgerdemain, 2026-10-10).
   No Salary Slips for the history: the export holds no figures per slip (bexio's paystub overview
   answers 401, erp-fxa7), and a slip with invented or zero amounts would feed the payroll hand-over
   report and the Lohnausweis with wrong numbers. The 89 paystub PDFs are private Files on their
   Employees, named `<year>-<month>-paystub.pdf` and keyed `paystub-<employee>-<year>-<month>`, so a rerun
   attaches nothing twice. The monthly payroll totals stay in the GL (`import_payroll.py`). The Lohnausweis
   for years before ERPNext payroll stays bexio's. Check: no Salary Slip exists for these months, and the
   Files post no GL. Figures per month, should they be wanted later, are a separate decision (every wage
   type would need a component first).
6. **Lohnausweis check.** Compare the erpnextswiss Salary Certificate layout with the 2026
   Form 11. Compare one year's output with the bexio paystub totals (counts and totals in
   the note).
7. **Net salary pay.** erpnextswiss salary payment proposal with HRMS, on a copy. Test the
   pain.001 file against the schema check in `bi_finance/payment_run.py`. Link to erp-xpmr.
8. **Cash forecast.** The forecast reads the salary accounts 5000 to 5099 (`cash_forecast.py`).
   After HRMS, the Payroll Entry accruals post to the same accounts. Confirm the payroll line
   of the forecast does not count a salary twice once the net pay runs through the bank.
   done: erp-b1dv, the forecast's payroll line is the slips' net once HRMS runs; the
   insurers' bills carry the contributions.
9. **Quellensteuer** (only if Question 2 says we build it). Its own bead, with the tariff
   tables as data.
10. **ELM** (only if Question 1 says we transmit ourselves). Its own bead, after a
    Swissdec-certification check.

Risks:

- **Privacy.** Payroll data is the most sensitive data in the company, and the repo is public.
  Every payroll file stays in `private/`. Tests use invented employees.
- **Install is a schema change.** HRMS adds many doctypes and custom fields on install and
  migrate. Install on a copy first; the rollback is the backup.
- **Image size and build time** grow with HRMS. Measure on the copy build, not from this study.
- **Lohnausweis: field check done, two gaps open.** The erpnextswiss doctype was written for its own sample
  companies. Each numbered line is checked against the 2026 Wegleitung in "Lohnausweis: field check". The gaps there (whole francs on the certificate, the KTG remark on line 15) are open.
- **ELM and certification.** A payroll that is not Swissdec-certified cannot send ELM data.
  This is a decision, not a build step.
- **Double booking.** The history rule above depends on the accrual filter in
  `get_sal_slip_list`. The check in step 5 is the test of it, and must pass before any
  history is loaded live.

## Recommendation

Go with HRMS `v16.50.0`, pinned as a tag. Write the Swiss deductions as Salary Components
with formulas (AHV, ALV, BVG, UVG/NBU, KTG, FAK) as settings, in the `bi_payroll` app (its fixtures and Payroll Swiss Settings).
Use erpnextswiss's Salary Certificate for the Lohnausweis, after its field layout is checked
against the 2026 Form 11. Load the bexio history as submitted Salary Slips with no
`payroll_entry`, so no GL is posted twice. Do not start the live site
until the two gaps have a decision: how ELM is transmitted, and how Quellensteuer is
calculated.

## Questions for Benchi

1. **ELM.** Transmit the Lohnmeldung ourselves, or through a Swissdec-certified payroll
   provider that takes our data? The second is the simpler route; the first needs a
   certification of the stack, and we do not know yet whether that is possible for ERPNext
   with HRMS. Who is the Ausgleichskasse and the tax office, and what do they accept?
2. **Quellensteuer.** Do any employees fall under source tax? If yes: build the tariff in
   `bi_finance`, or have the calculation done outside the system and enter the amount?
3. **BVG.** Which Pensionskasse, and its plan's rates by age band, employer and employee
   split? Is the insured salary fixed or does it follow the salary?
4. **Insurers and funds.** The names of the Ausgleichskasse, the UVG, NBU and KTG insurer,
   and the FAK fund, each with its current rate sheet. The rates are in these, not in the
   law.
5. **13th salary.** Answered 2026-10-11 from the figures (HR 3): none. No 13th salary, bonus or December double
   in the 35 months of the figures (9 employees). No component; a "13th Salary" component, paid in December
   through Additional Salary, is added only when a contract says so. Counts in `finance/apps/bi_payroll/README.md`.
6. **History depth.** Which business years must the Lohnausweis cover from ERPNext? Each year
   back is one load of Salary Slips from bexio.
7. **Net salary payment.** Keep it on the UBS pain.001 run (erp-xpmr), once it works with
   HRMS? Or pay salaries by hand?
8. **Install order.** OK to install HRMS on a copy site first, and to deploy the live site
   only after the copy passes and in a slot ledgerdemain gives?

## Lohnausweis map (erp-fs9c)

The report `Payroll hand-over` (bi_payroll) has a view `Lohnausweis` that sums the same rows into the
Form 11 lines (column in brackets). The line numbers and labels are the fields of erpnextswiss's Salary
Certificate, which the settings step `lohnausweis` (`finance/scripts/swiss-setup.py`) maps to our components.
Lines 1, 11 and 15 follow the ESTV Wegleitung 2026 (read for erp-hx7s: Ziffer 1, 10.1, 11 and 15), with
tests (erp-4yyn): line 1 carries every earning row of the slips; on the certificate, line 11 is line 8 less
lines 9, 10.1 and 10.2 (not the slips' net pay, which also deducts Quellensteuer and KTG); and the KTG employee share goes into
line 15 as a remark. The view keeps rappen; the flag 'Whole francs' rounds it for the export; the certificate itself does not round yet (see "Lohnausweis: field check")
(round half up). The full line-by-line check on the copy stays step 6 of the plan. erpnextswiss's Salary
Certificate gets the same net rule through our patches (`finance/patches/erpnextswiss`, 0002-0004).

The step `lohnausweis` sets each table by the component's name (column 2, the fixture's name), not by abbr: the
live-era standard component 'Basic' holds abbr B from before bi_payroll, so the fixture's 'Basic Salary' gets B_1 and
an abbr lookup would find 'Basic', and line 1 would be 0. When a second component holds one of the fixtures' abbrs, the
step prints `lohnausweis: abbr B is held by 'Basic', ...` and changes nothing else. Run live 2026-10-11 (erp-zg4p);
the abbr question it printed was decided: 'Basic' keeps B (the settings map by name, nothing reads abbr B) — left
as is, cosmetic.

Checked against ESTV, Wegleitung zum Ausfüllen des Lohnausweises 2026 (Formular 11, the 2027 edition
when published): **yes** = the Wegleitung's text for the line was read and the source below fits it;
**heading** = only the line's heading was checked, so the line is not in payroll and the trustee reads its text.
The ESTV PDF is kept under `private/payroll/forms/` (not in the repo).

| Form line                                                  | Salary Component(s) on the slip                                      | Column in view `Lohnausweis` | Status                                                                                                                  |
| ---------------------------------------------------------- | -------------------------------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 1. Lohn / Rente                                            | every earning row of the slip (Basic Salary, Commission, any Zulage) | `line_1`                     | in payroll (Wegleitung Ziffer 1: all employer payments not on lines 2-7), erp-4yyn                                      |
| 2.1 Verpflegung und Unterkunft                             | none                                                                 | —                            | not in payroll                                                                                                          |
| 2.2 Privatanteil Geschäftsfahrzeug                         | none                                                                 | —                            | not in payroll                                                                                                          |
| 2.3 Weitere Gehaltsnebenleistungen                         | none                                                                 | —                            | not in payroll                                                                                                          |
| 3. Unregelmässige Leistungen                               | none                                                                 | —                            | not in payroll                                                                                                          |
| 4. Kapitalleistungen                                       | none                                                                 | —                            | not in payroll                                                                                                          |
| 5. Beteiligungsrechte                                      | none                                                                 | —                            | not in payroll                                                                                                          |
| 6. Verwaltungsratsentschädigungen                          | none                                                                 | —                            | not in payroll                                                                                                          |
| 7. Andere Leistungen                                       | none                                                                 | —                            | not in payroll                                                                                                          |
| 8. Bruttolohn total                                        | gross pay of the slip                                                | `line_8`                     | in payroll                                                                                                              |
| 9. Beiträge AHV/IV/EO/ALV/NBUV                             | AHV/IV/EO Employee, ALV Employee, NBU Employee                       | `line_9`                     | in payroll                                                                                                              |
| 10.1 Ordentliche Beiträge berufliche Vorsorge              | BVG Employee                                                         | `line_10_1`                  | in payroll                                                                                                              |
| 10.2 Beiträge Einkauf berufliche Vorsorge                  | none                                                                 | —                            | not in payroll                                                                                                          |
| 11. Nettolohn / Rente                                      | line 8 less lines 9 and 10.1, computed in the view                   | `line_11`                    | in payroll (Wegleitung Ziffer 11). The slip's net pay is lower: it also deducts Quellensteuer and KTG, erp-4yyn         |
| 12. Quellensteuerabzug                                     | Quellensteuer Employee                                               | `line_12`                    | in payroll since erp-6rrh (the tax is set on the slip from the gross, per canton tariff)                                |
| 13.1.1 Effektive Spesen Reise / Verpflegung / Übernachtung | none                                                                 | —                            | not in payroll                                                                                                          |
| 13.1.2 Effektive Spesen übrige                             | none                                                                 | —                            | not in payroll                                                                                                          |
| 13.2.1 Pauschalspesen Repräsentation                       | none                                                                 | —                            | not in payroll                                                                                                          |
| 13.2.2 Pauschalspesen Auto                                 | none                                                                 | —                            | not in payroll                                                                                                          |
| 13.2.3 Pauschalspesen übrige                               | none                                                                 | —                            | not in payroll                                                                                                          |
| 13.3 Beiträge an die Weiterbildung                         | none                                                                 | —                            | not in payroll                                                                                                          |
| 14. Weitere Gehaltsnebenleistungen                         | none                                                                 | —                            | not in payroll                                                                                                          |
| 15. Bemerkungen                                            | KTG Employee, as the remark "KTG-Beitrag Arbeitnehmer CHF …"         | `line_15`                    | in payroll (Wegleitung Ziffer 15): KTG has no line of its own, erp-4yyn. KTG keeps its own column in the hand-over view |

| Form line                                                 | Source in ERPNext                                                          | erpnextswiss field                  | Checked     | Note                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A Lohnausweis (box)                                       | Certificate type                                                           | `certificate_type` = Salary         | yes         | Set by default. Pension (B, Rentenbescheinigung) stays unset                                                                                                                                                                                                                                                                                               |
| C AHV-Nummer / Geburtsdatum                               | Employee                                                                   | print format only                   | yes         | Printed from the employee; not a field of the certificate                                                                                                                                                                                                                                                                                                  |
| F Unentgeltliche Beförderung (box)                        | Checkbox                                                                   | `free_transport`                    | yes         | **no**, unless Benchi says otherwise. Ticked only for free commute (Rz 21-25, GA with business need); a commute refund goes to 2.3                                                                                                                                                                                                                         |
| G Kantinenverpflegung / Lunch-Checks (box)                | Checkbox                                                                   | `lunch_checks`                      | yes         | **no**, unless Benchi says otherwise. Meal allowances in cash are line 1 (Rz 18), not this box                                                                                                                                                                                                                                                             |
| H Name und Adresse                                        | Employee                                                                   | print format only                   | yes         | Current residential address (`permanent_address`)                                                                                                                                                                                                                                                                                                          |
| Jahr, Ein-/Austritt                                       | Certificate `year`, `start_date`, `end_date`                               | `year`, `start_date`, `end_date`    | yes         | Set on the certificate. The period is printed even for a full year                                                                                                                                                                                                                                                                                         |
| 1. Lohn / Rente                                           | Salary Component `Basic Salary` (abbr B)                                   | `salary`                            | yes         | `line_1` in the view. The Wegleitung counts every Zulage, commission and Taggeld paid by the employer here (Rz 13-18): the view sums every earning row of the slips, and the settings table `salary` names every Earning component (Basic Salary, Commission; a new Earning component joins `LOHNAUSWEIS` in `swiss-setup.py`, a test guards it), erp-4yyn |
| 2.1 Verpflegung und Unterkunft                            | none                                                                       | `catering`                          | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 2.2 Privatanteil Geschäftsfahrzeug                        | none                                                                       | `car`                               | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 2.3 Weitere Gehaltsnebenleistungen                        | none                                                                       | `other_salary`                      | heading     | not in payroll; trustee. Also the commute refund (Rz 17) and a GA without business need (Rz 19)                                                                                                                                                                                                                                                            |
| 3. Unregelmässige Leistungen                              | none                                                                       | `irregular`                         | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 4. Kapitalleistungen                                      | none                                                                       | `capital`                           | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 5. Beteiligungsrechte                                     | none                                                                       | `participation`                     | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 6. Verwaltungsratsentschädigungen                         | none                                                                       | `board`                             | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 7. Andere Leistungen                                      | none                                                                       | `other`                             | heading     | not in payroll; trustee. Also "Quellensteuer vom Arbeitgeber bezahlt", if the employer pays the tax                                                                                                                                                                                                                                                        |
| 8. Bruttolohn total                                       | Gross pay of the submitted slips                                           | `gross_salary` (from `gross_pay`)   | yes         | The Wegleitung: total of lines 1 to 7. Equal to line 1 while only `Basic Salary` is an earning                                                                                                                                                                                                                                                             |
| 9. Beiträge AHV/IV/EO/ALV/NBUV                            | `AHV/IV/EO Employee`, `ALV Employee`, `NBU Employee` (abbr AHV, ALV, NBUV) | `deduction_ahv`                     | yes         | Employee shares only; employer shares are not on the form                                                                                                                                                                                                                                                                                                  |
| 10.1 Ordentliche Beiträge BVG                             | `BVG Employee` (abbr PK)                                                   | `deduction_pension`                 | yes         | Ordinary contributions, as deducted from the gross                                                                                                                                                                                                                                                                                                         |
| 10.2 Beiträge Einkauf BVG                                 | none                                                                       | `deduction_pension_additional`      | heading     | not in payroll; trustee (buy-ins are paid by the employee outside payroll, Rz 46)                                                                                                                                                                                                                                                                          |
| 11. Nettolohn / Rente                                     | Line 8 less lines 9 and 10.1 (patch 0002; upstream: net pay of the slips)  | `net_salary`                        | **patched** | Upstream differs from the Wegleitung, which defines line 11 as line 8 less lines 9 and 10 (Ziffer 11): `net_pay` also subtracts Quellensteuer and any KTG or other deduction. Our patch 0002 computes it as line 8 less 9 and 10.1 (erp-4yyn, below)                                                                                                       |
| 12. Quellensteuerabzug                                    | `Quellensteuer Employee` (abbr QST), gross tax                             | `source_tax_deduction`              | yes         | Only for foreign employees with the tax at source; the tax is set on the slip from the gross per canton tariff (erp-6rrh)                                                                                                                                                                                                                                  |
| 13.1.1 Effektive Reise-/Verpflegungs-/Übernachtungsspesen | none                                                                       | `effective_expenses_travel`         | heading     | not in payroll; trustee. Its box (`effective_expenses_travel_check`) is **no** unless a Spesenreglement is approved (Rz 52)                                                                                                                                                                                                                                |
| 13.1.2 Effektive übrige Spesen                            | none                                                                       | `effective_other_expenses`          | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 13.2.1 Pauschalspesen Repräsentation                      | none                                                                       | `representation_expenses`           | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 13.2.2 Pauschalspesen Auto                                | none                                                                       | `car_expenses`                      | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 13.2.3 Pauschalspesen übrige                              | none                                                                       | `other_net_expenses`                | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 13.3 Beiträge Weiterbildung                               | none                                                                       | `education`                         | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 14. Weitere Gehaltsnebenleistungen                        | none                                                                       | `other_salary_sides`                | heading     | not in payroll; trustee                                                                                                                                                                                                                                                                                                                                    |
| 15. Bemerkungen                                           | Free text on the certificate                                               | `remarks`                           | yes         | The settings' remark table stays empty; the view fills line 15 itself with the KTG remark (erp-4yyn). Other mandatory notes (several certificates in one year, Privatanteil) are typed by the trustee                                                                                                                                                      |
| (no line) KTG Employee                                    | `KTG Employee` (abbr KTG)                                                  | the view's line-15 remark, erp-4yyn | yes         | The Wegleitung (Ziffer 9): employee-paid Krankentaggeld contributions may not be deducted from the gross; the view remarks them under line 15 ("KTG-Beitrag Arbeitnehmer CHF …"). The trustee confirms before a deduction is used                                                                                                                          |

Unentgeltliche Beförderung and Kantinenverpflegung (boxes F and G) are not in payroll: both boxes stay **no**.
The employer shares (AHV, ALV, BVG, UVG, KTG, FAK) are not on the form's lines and stay in the
hand-over view only.

**Decided (ledgerdemain, 2026-10-11), built in erp-4yyn:**

1. **Line 11** = line 8 less lines 9 and 10.1 (Wegleitung Ziffer 11), not the slip's `net_pay`: the fetch (erpnextswiss `fetch_dynamic`, patch 0002) and the view `Lohnausweis` both follow it.
2. **Line 1** = every taxable earning on the slip (Basic Salary, Commission, Zulagen), not `Basic Salary` alone. The view sums every earning row; the settings table `salary` names Basic Salary and Commission.
3. **KTG**: the employee's share goes into line 15 as a remark, not onto a line.
4. **Rounding**: the view and the export round to whole francs (round half up); the view keeps rappen. The certificate itself still prints rappen (gap, see "Lohnausweis: field check").

### Lohnausweis at year end (operating)

Once per live site, after the go-live bead: `finance/scripts/swiss-setup.sh lohnausweis` (idempotent; it sets
Salary Certificate Settings to the mapping above and warns if BI Concepts has no primary address). Its
dynamic configuration is needed: the static mode has no table for line 12. The first run is done (erp-zg4p,
2026-10-11); reruns say in place.

The Salary Certificate is on ERPNext's rail under Swiss Payroll, section Year end (bi_payroll's sidebar, claimed, so
it opens there; the erpnextswiss shell is not reached from the rail).

Each January, for the year that just closed (the first is 2027, the 2026 certificates are bexio's):

1. Check the year's view: report `Payroll hand-over`, view `Lohnausweis`, filter the year. Counts only go in notes.
2. Per employee with submitted slips in the year: a new **Salary Certificate**, the employee, the year, the
   period (1 January to 31 December, or the exit date), certificate type Salary. Then **Werte holen** (fetches the
   lines from the slips by posting date).
3. Compare the lines with the view: equal per line (the view with the flag 'Whole francs' for the certificate's francs).
4. Boxes F and G stay **no**, and the box for 13.1.1 too, unless Benchi says otherwise. Line 15: free text,
   for example a second certificate of the year ("one of two" per Wegleitung Ziffer 15).
5. Submit, then **PDF** (print format `605.040.18N Form. 11`) and hand the PDFs to the trustee with the view.
6. The trustee checks lines 2 to 7, 10.2, 13 and 14, the decisions above, and the printed layout.

An employee who left mid-year gets a certificate for the part of the year paid by us, with the exit date in the
period. A certificate created in error is cancelled and amended, never deleted.

## Lohnausweis: field check

Read-only check (erp-8tgp, 2026-10-22) of erpnextswiss' Salary Certificate against the official Form 11, from
public sources only. No employee, salary or private/ data is used here.

**Sources**

- Form and Wegleitung: ESTV and SSK, _Wegleitung zum Ausfüllen des Lohnausweises bzw. der Rentenbescheinigung
  (Formular 11)_, Wegleitung 605.040.18.1d, gültig ab 1. Januar 2026:
  <https://www.estv.admin.ch/dam/de/sd-web/afP1GDFr8gE3/dbst-form-lohna-wegleitung-2026-de.pdf>.
  "Rz" below is the Randziffer of this edition. The Formular is 605.040.18 (its page 2 carries the code
  `605.040.18N 01.21`). The 2027 edition is not published yet; check again when it is.
- erpnextswiss at the pin in `finance/apps.json` (`libracore/erpnextswiss`, branch `v16`, commit `5d85c45`):
  `erpnextswiss/doctype/salary_certificate/salary_certificate.json` and `.py`, and the print format
  `print_format/605.040.18n_form._11/`. Our patches on top: `finance/patches/erpnextswiss` 0002 (line 11), 0003
  (component SQL) and 0004 (import).

**Map.** Field names are those of the upstream doctype. "Settings" means the Salary Certificate Settings step
`lohnausweis` (`finance/scripts/swiss-setup.py`).

| Form line                                                 | Wegleitung                                   | Salary Certificate field                                           | Filled from                                                 | Result                                                                                     |
| --------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| A Lohnausweis (box)                                       | Rz 4                                         | `certificate_type` = Salary                                        | default                                                     | yes                                                                                        |
| B Rentenbescheinigung (box)                               | Rz 5                                         | `certificate_type` = Pension                                       | by hand                                                     | not used                                                                                   |
| C AHV-Nr. / Geburtsdatum                                  | Rz 6                                         | none (print format)                                                | Employee AHV number and birth date                          | yes                                                                                        |
| D Kalenderjahr                                            | Rz 7                                         | `year`                                                             | by hand                                                     | yes; several certificates: remark, Rz 66                                                   |
| E Lohnperiode                                             | Rz 8                                         | `start_date`, `end_date`                                           | by hand                                                     | yes                                                                                        |
| F Unentgeltliche Beförderung (box)                        | Rz 9                                         | `free_transport`                                                   | default 0                                                   | yes; stays 0 (not in payroll)                                                              |
| G Kantinenverpflegung / Lunch-Checks (box)                | Rz 10                                        | `lunch_checks`                                                     | default 0                                                   | yes; stays 0 (not in payroll)                                                              |
| H Name und Adresse                                        | Rz 11                                        | print format: `employee_name`, `permanent_address`                 | Employee                                                    | yes; check the address is the current one                                                  |
| I Ort, Datum, Unterschrift                                | Rz 12                                        | none (on the form image)                                           | by hand                                                     | yes                                                                                        |
| 1 Lohn                                                    | Rz 13–18                                     | `salary`                                                           | Settings: the Earning components (Basic Salary, Commission) | yes; a new Earning component must join the settings list (a test guards it)                |
| 2.1 Verpflegung und Unterkunft                            | Rz 20                                        | `catering`                                                         | Settings                                                    | heading only: no component                                                                 |
| 2.2 Privatanteil Geschäftsfahrzeug                        | Rz 21–25                                     | `car`                                                              | Settings                                                    | heading only: no company car                                                               |
| 2.3 Weitere Gehaltsnebenleistungen                        | Rz 26 (commute refund Rz 17)                 | `other_salary`, `other_salary_description`                         | Settings                                                    | heading only                                                                               |
| 3 Unregelmässige Leistungen                               | Rz 27                                        | `irregular`, `irregular_description`                               | Settings                                                    | heading only: bonuses belong in line 1 (Rz 27)                                             |
| 4 Kapitalleistungen                                       | Rz 28                                        | `capital`, `capital_description`                                   | Settings                                                    | heading only                                                                               |
| 5 Beteiligungsrechte                                      | Rz 29                                        | `participation`                                                    | Settings                                                    | heading only                                                                               |
| 6 Verwaltungsratsentschädigungen                          | Rz 30                                        | `board`                                                            | Settings                                                    | heading only                                                                               |
| 7 Andere Leistungen                                       | Rz 31–40                                     | `other`, `other_description`                                       | Settings; by hand                                           | **differs**: employer-paid Quellensteuer (Rz 39, 48) has no component; typed by hand       |
| 8 Bruttolohn total                                        | Rz 41                                        | `gross_salary`                                                     | `gross_pay` of the submitted slips                          | yes; equal to line 1 while Basic Salary and Commission are the only earnings               |
| 9 Beiträge AHV/IV/EO/ALV/NBUV                             | Rz 42                                        | `deduction_ahv`                                                    | Settings: employee shares of AHV/IV/EO, ALV, NBU            | yes                                                                                        |
| 10.1 Ordentliche Beiträge BVG                             | Rz 44                                        | `deduction_pension`                                                | Settings: BVG Employee                                      | yes                                                                                        |
| 10.2 Beiträge Einkauf BVG                                 | Rz 45–46                                     | `deduction_pension_additional`                                     | Settings                                                    | heading only: buy-ins are outside payroll (Rz 46)                                          |
| 11 Nettolohn                                              | Rz 47 (line 8 less Ziffer 9 and 10)          | `net_salary`                                                       | patch 0002: gross less 9, 10.1 and 10.2                     | yes, patched: upstream uses the slip's `net_pay`, which also deducts Quellensteuer and KTG |
| 12 Quellensteuerabzug (brutto)                            | Rz 48                                        | `source_tax_deduction`                                             | Settings: Quellensteuer Employee                            | yes                                                                                        |
| 13 Spesenvergütungen                                      | Rz 49–51                                     | none                                                               | not in payroll                                              | heading only                                                                               |
| 13.1.1 Effektive Reise-/Verpflegungs-/Übernachtungsspesen | Rz 52–53                                     | `effective_expenses_travel`, `effective_expenses_travel_check`     | Settings; by hand (check box)                               | heading only; check box stays 0 without an approved Spesenreglement (Rz 52)                |
| 13.1.2 Übrige effektive Spesen                            | Rz 54                                        | `effective_other_expenses`, `effective_other_expenses_description` | Settings                                                    | heading only                                                                               |
| 13.2.1 Pauschalspesen Repräsentation                      | Rz 56                                        | `representation_expenses`                                          | Settings                                                    | heading only                                                                               |
| 13.2.2 Pauschalspesen Auto                                | Rz 57                                        | `car_expenses`                                                     | Settings                                                    | heading only                                                                               |
| 13.2.3 Übrige Pauschalspesen                              | Rz 58                                        | `other_net_expenses`, `other_net_expenses_description`             | Settings                                                    | heading only                                                                               |
| 13.3 Beiträge Weiterbildung                               | Rz 61                                        | `education`                                                        | Settings                                                    | heading only                                                                               |
| 14 Weitere Gehaltsnebenleistungen (no amount)             | Rz 62                                        | `other_salary_sides`                                               | by hand                                                     | heading only                                                                               |
| 15 Bemerkungen                                            | Rz 63; KTG Rz 42; several certificates Rz 66 | `remarks`                                                          | Settings remark table (empty) and prefix                    | **differs**: the KTG remark is not on the certificate (see below)                          |
| (no line) KTG Arbeitnehmeranteil                          | Rz 42 (Rz 42 allows it in line 15)           | not a field                                                        | bi_payroll view only (`hand_over.py`)                       | **differs**: the certificate omits the remark                                              |
| Formular: "Nur ganze Frankenbeträge"                      | Formular, page 2                             | all Currency fields                                                | none: no precision and no rounding in the print format      | **differs**: rounding is in bi_payroll's view and export only (`certificate_rounding`)     |

**Print format.** `605.040.18N Form. 11` draws the form as a background image, `605_040_18N_Form_11.png` from
erpnextswiss, and places the values on it in the form's order. The Wegleitung makes Formular 11 and its rules
binding for the Lohnausweis (Rz 1) and names printing it from payroll software on white paper as the normal way
(Rz 75). Its "own form with equivalent content" (Rz 2) is for pension funds issuing a Rentenbescheinigung, so the
employer's certificate keeps the official layout. Our patch 0007 (`form11-fits-a4`) scales the sheet
(`zoom: 0.69`) so it fits A4; the values keep their places on the image. The image is not compared pixel by
pixel with the ESTV PDF: check the first printed PDF by eye (erp-rqxs proof).

**Verdict: usable with gaps.** Every numbered line and box maps to a field. Two gaps change the printed
certificate: whole francs, and the KTG remark on line 15. Both are fixable in a later bead, not here.

**Gaps (for ledgerdemain to file)**

1. Whole francs: the certificate prints rappen. Form 11 asks for whole francs only (`certificate_rounding`
   exists in bi_payroll's view and export, not on the certificate). Decide: round on the certificate, or
   require the export.
2. Line 15: the KTG employee share is in the view only. The certificate gets the prefix, not the remark. Fill
   the settings remark table, or add the remark to the certificate.
3. Line 7: employer-paid Quellensteuer (Rz 39, 48) has no component. Typed by hand today; a component only if
   the company ever pays it.
4. Line 11 in the view: `bi_payroll` subtracts lines 9 and 10.1 only; patch 0002 and Rz 47 subtract 10.2 too.
   The two agree while 10.2 is empty (it is outside payroll); align the view before any buy-in reaches payroll.
5. Check, not a gap: box H uses the Employee's `permanent_address`, which must be the current residential address
   (Rz 11). Check it on the first certificate.
6. Check, not a gap: re-read the Wegleitung when the 2027 edition is published.

## Copy site

A copy of the live books on a train image, so the HR train's children can prove their work
before it reaches live. The live site runs live's code and proves nothing for the train, so the
copy is a separate stack: the compose project `frappe-finance-copy`, with its own volumes and
network, started on demand. It holds the full books, so it stays on this Mac mini. It listens on
`127.0.0.1:8815` only. Nothing is on the tailnet until the yardmaster serves it with
`tailscale serve` on the next free finance tailnet port (from 8462) when Benchi needs to look.
The live stack is not touched by any of this.

- Script: `finance/scripts/copy-stack.sh` runs every step below. It refuses the live project
  (`frappe-finance`) and an image that is not built, and before `up` it checks that the VM has
  2.5 GiB free (MemAvailable).
- A setup step can be rehearsed through the same script with `FINANCE_PROJECT=frappe-finance-copy`.
- Image: `frappe-finance-custom:<tag>`, the train image, built from `finance/apps-copy.json`
  with `finance/scripts/build-image.sh copy <tag>` (see `swiss.md`, Build). `finance-copy.yml`
  takes the tag from `COPY_IMAGE`, which the script sets. The live image is built from
  `finance/apps.json`: `finance/scripts/build-image.sh live <tag>`.
- Every `up` and `refresh` takes a fresh backup of the live site (it only reads the books),
  stages its four files in `private/copy-restore/` (mode 700), copies them into the copy and
  restores them. The restore brings the live site config and database back, so the safety
  settings below are set after every restore. Do not turn them off.
- Safety settings, after every restore: `pause_scheduler` 1 and `mute_emails` 1 (site config: no
  scheduled jobs, no mail). `migrate` runs after the restore, because the train image may carry
  patches the live site has not run; it runs with `--skip-search-index`, because migrate enqueues the website
  search index and a lean copy has no queue to take it. PDFs work on the copy because `safety()` sets `host_name` to
  the copy's nginx at `http://frontend:8080`. Wise Settings and PayPal Settings `sync_enabled` 0 (the
  hourly bank syncs). Every Email Account has outgoing and incoming off. Every Webhook is
  disabled. Website Settings `app_name` is "COPY - finance copy, not live", the navbar brand.
  bexio has no sync setting in bi_finance (its data comes in by the importers only), so none is
  switched off.
- The copy's Administrator password is in `private/.erpnext-admin-copy` (mode 600). The script
  makes it the first time. It is never in the repo, and `finance-copy.yml` holds no secrets.
- The copy's frontend port is 8815, from the finance reserve. `integrations/PORTS.md` lists the
  old copy's 8081 for the same purpose, and IT decides whether 8815 is added there.

Run from the repository root, with the copy's image built:

**Start** (a fresh live backup; the first start creates the site, which takes a few minutes)

```sh
finance/scripts/copy-stack.sh up <tag> [--jobs] [--websocket]
```

Without flags the copy starts lean: db, redis-cache, backend and frontend, plus the one-shot configurator and
create-site. That is what a Desk test needs. On the first start create-site waits up to two minutes for
redis-queue, which is not up yet, and then goes on.

- `--jobs` for a step that enqueues work: redis-queue, queue-short, queue-long and the scheduler start after
  the restore and the safety settings, so the scheduler is already paused. The enqueue points on the copy:
  - hrms Payroll Entry (`hrms/payroll/doctype/payroll_entry/payroll_entry.py`): create salary slips, submit
    salary slips, and Overtime Slip create and submit, in the background above 30 employees; cancel above
    50 linked salary slips.
  - bi_finance and bi_payroll have no `frappe.enqueue` of their own. Their hourly Wise and PayPal syncs and
    the monthly revaluation run from the scheduler, which stays paused on the copy.
  - Prepared reports: none (every report of the two apps has `prepared_report` 0). Mails are muted here.
- `--websocket` for realtime updates in the Desk (progress bars). frontend starts websocket anyway, because
  its nginx resolves the websocket host at start, so on the frontend path the flag changes nothing.

The lean start is not run on a copy yet: the stub-docker tests in `finance/scripts/test_copy_stack.py` prove
the calls, and the first real start proves the restore and migrate without redis-queue.

Down when the test is done (Benchi via IT, 2026-10-12: everything on the mini runs lean).

**Refresh** (a fresh live backup replaces the copy's books; the copy must be up)

```sh
finance/scripts/copy-stack.sh refresh
```

**Stop** (keeps the volumes)

```sh
finance/scripts/copy-stack.sh down
```

**Remove** (deletes the copy's books on this machine: its volumes, the staged backups in
`private/copy-restore/` and the Administrator password file)

```sh
finance/scripts/copy-stack.sh remove
```

**Tailnet** (the yardmaster, only when Benchi needs to look; the port goes in `integrations/PORTS.md`)

```sh
/opt/homebrew/bin/tailscale serve --bg --https=<next free finance port> http://127.0.0.1:8815
/opt/homebrew/bin/tailscale serve --https=<same port> off
```

Every compose command the script runs names the project `frappe-finance-copy` and the override
file `finance-copy.yml`. Never `-p frappe-finance` here: that is the live stack.

## Sources

- HRMS: `frappe/hrms` branch `version-16`, tag `v16.50.0`, read through the GitHub API.
- ERPNext: `frappe/erpnext` tag `v16.50.0` (`7474d9e`).
- erpnextswiss: `libracore/erpnextswiss` branch `v16` (`README_V16.md`, `custom/employee.json`,
  `custom/salary_slip.json`, `doctype/salary_certificate*`, `doctype/payment_proposal_salary_slip`,
  `templates/print_formats/kt_salary_slip.html`).
- Swiss public values: the links in the table, from a web search on 2026-10-10. Those are
  secondary sources, except the BFS and Swissdec pages. The Form 11 Wegleitung was not opened.
