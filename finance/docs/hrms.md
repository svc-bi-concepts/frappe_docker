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

**Decision 2026-10-10: payroll runs on the live site.** The live ERPNext is a test environment,
so HRMS `v16.50.0` and `bi_payroll` are installed on the live site `frontend`, not only the copy.
This replaces the rule "live = base + bi_finance, HRMS on the copy only". `apps.json` lists hrms,
and `build-image.sh` builds `bi_payroll` on top of `bi_finance`. The copy stack stays as a sandbox.

**Open:** certified ELM transmitter: inquiry later.

The hand-over rows are checked against invented employees (`python3 -m unittest
bi_payroll.test_hand_over`) and were run on a throwaway site with invented slips (erp-fs9c: rows
equal the slips per year and per month). Two parts are open and stay so: Quellensteuer (in payroll since erp-6rrh,
a component set from the gross) was not yet on the site the proof ran on, so its column there is 0, and the
Lohnausweis line mapping (below) is not checked against the 2026 form (step 6).

## Versions

| Item | Pin | Evidence |
| --- | --- | --- |
| ERPNext | `v16.50.0`, commit `7474d9e` (live) | tag, as in `apps.json` and `swiss.md` |
| Frappe | `v16.50.0`, commit `f20f92d` | as in `swiss.md` |
| HRMS | tag `v16.50.0`, commit `7c03769` (branch `version-16`, head 2026-10-07) | `gh api repos/frappe/hrms/git/refs/tags/v16.50.0` |
| HRMS requires | `frappe/erpnext` (`required_apps` in `hrms/hooks.py`); Python `>=3.10` | `pyproject.toml` on `version-16` |

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

| Need | How it is met | 2026 public values | Status | Source |
| --- | --- | --- | --- | --- |
| AHV/IV/EO | Salary Components with a formula on the gross AHV wage: 5.30% employee, 5.30% employer | 10.60% total, 5.30% each side. The sources do not agree on the split between AHV, IV and EO; the formula does not need it | Config, no code. Check the wage base (family allowances excluded) with the Ausgleichskasse | [weka](https://www.weka.ch/themen/personal/lohn-und-gehalt/lohnabrechnung/article/lohnabzuege-die-aktuellen-sozialversicherungsbeitraege/), [onlinetreuhand 2026](https://www.onlinetreuhand.ch/fileadmin/user_upload/Onlinetreuhand/Dokumente/Sozialversicherungen_2026.pdf) |
| ALV (incl. ceiling) | Component with `min(base, ceiling)`: 1.10% employee, 1.10% employer | 2.20% total up to CHF 148,200 a year (unchanged from 2025). No contribution above the ceiling: the 1% solidarity contribution (ALV 2) lapsed on 1 Jan 2023 | Config, no code. Nothing above the ceiling | same sources; [law.ch](https://law.ch/lawnews/2022/10/arbeitslosenversicherung-solidaritaetsprozent-entfaellt-per-01-01-2023/) |
| BVG (coordinated salary) | Component on `salary - coordination deduction`, with an entry threshold, and the age-based rates of the Pensionskasse. The split and the rates are the fund's plan, not the law | Entry threshold CHF 22,680; coordination deduction CHF 26,460 | Config is possible (the formula reads the employee's date of birth). The plan rates are open | [convit](https://convit.ch/wissen/bvg-koordinationsabzug), [auditrium](https://www.auditrium.ch/blog/articles/koordinationsabzug-2026-wichtige-aenderungen-fuer-unternehmen) |
| UVG / NBU | Components with the insurer's percentage on the insured salary. BU/UVG employer share, NBU employee share | Set by the insurer per policy | Config. Needs the policy sheets | none; insurer documents |
| KTG | Component with the insurer's rate on the insured salary | Set by the insurer | Config. The 2026 KTG contract is fetched and read (erp-1jh5, 2026-10-11): its values sit in the private CSV and the settings stay open for the follow-up bead; the employer pays the premium, so the sheet names no employee share | the contract in `private/payroll-insurance/` |
| FAK | Employer component on the AHV wage, rate by canton and Ausgleichskasse | Set by the fund | Config. Needs the fund's rate | none |
| Quellensteuer | Not covered. erpnextswiss's Salary Certificate settings have a field "12 Quellensteuerabzug" that maps a component; it does not compute the tax | Cantonal tariffs, per year | **Gap.** Build (bi_finance) or outsource. See Questions | `erpnextswiss` `salary_certificate_settings.json` |
| 13th salary | A Salary Component paid in December, or an accrual over the year (`accrual_component`) | Contract | None in the data (Question 5). No component until a contract says so | HRMS `salary_component` fields |
| Lohnausweis (Form 11), per employee per year | erpnextswiss `Salary Certificate`: sums `Salary Slip` gross and net over the year, and the `Salary Detail` rows by `abbr` (B, AHV, ALV, NBUV, PK) into the form's fields. Fields are set in `Salary Certificate Settings` | ESTV Wegleitung for Form 11 applies from 1 Jan 2026 | Needs HRMS. The field layout is **not checked** against the 2026 form | [ESTV Wegleitung 2026](https://www.estv.admin.ch/dam/de/sd-web/afP1GDFr8gE3/dbst-form-lohna-wegleitung-2026-de.pdf) (not opened in this study; from the search result) |
| ELM / Swissdec transmission | Not in erpnextswiss `v16` (no ELM file in its tree), not in HRMS | ELM 4.0 switched off; ELM 5.0 required for the 2026 payroll year. Sources disagree on the 4.0 end date (30.06.2026 per BFS; end 2025 per one vendor). Transmission needs Swissdec-certified payroll software | **Gap.** See Questions and `elm.md` | [BFS](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html), [Swissdec](https://www.swissdec.ch/abschaltung-elm-4-0), [AXA](https://www.axa.ch/de/unternehmenskunden/melden-und-mutieren/melden/elektronische-lohnmeldung.html) |
| Payslip layout | HRMS print format on Salary Slip, built for us (the no-company-data rule keeps the template generic). erpnextswiss ships `kt_salary_slip.html`, a print for one of its own sample companies; use it as a reference only | none | Build. Small | erpnextswiss `templates/print_formats/kt_salary_slip.html` |
| Net salary payment (pain.001) | erpnextswiss `Payment Proposal` with `Payment Proposal Salary Slip` (the salary side of the payment run). The run in `payment-runs.md` leaves salaries out while the site has no HRMS | pain.001.001.09 as in `payment-runs.md` | Test on a copy after HRMS (erp-xpmr) | `payment-runs.md`; erpnextswiss `payment_proposal_salary_slip` |
| Employee AHV number | erpnextswiss custom fields `social_security_number` and `old_social_security_number` on Employee | n/a | Present in `v16` (`custom/employee.json`) | erpnextswiss `v16` |

## Migration from bexio

The bexio payroll export (bead erp-5sbz) is not run yet, so this section maps the entities
the bead asks for. Counts come with that export. Nothing below has been read from `private/`.

| bexio payroll | ERPNext / HRMS doctype | Note |
| --- | --- | --- |
| Employees | Employee (ERPNext core) | AHV number to `social_security_number`. Pay data is not on Employee |
| Salary types and insurance settings | Salary Component, Salary Structure | Set `abbr` to the codes erpnextswiss reads: B, AHV, ALV, NBUV, PK. Without these codes the Lohnausweis sums come out as zero |
| Paystubs (PDF, per employee, year, month) | Private File on the Employee (`import_paystubs.py`), no Salary Slip: the export has no figures | Reference only; the Lohnausweis of past years stays bexio's (step 5) |
| Absences per employee and business year | Leave Application (or a Leave Allocation balance) | Only for the open year, or as a balance. Per-day history is not needed for the Lohnausweis |
| Payroll journal lines (booked) | Journal Entry (already loaded by `import_payroll.py`, bexio_id `journal-<id>`) | Stays as the GL |

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
4. **Employee load from the bexio export.** Offline importer, the same shape as
   `import_master.py`, writing only Employee and the AHV number. Mocked data in tests.
   Done 2026-10-11 (erp-y909): `swiss-setup.sh fields` (Employee in `BEXIO_DOCTYPES`),
   `create-api-user.sh` (the API user's HR User role), backup `20261011_001457`, then
   `python3 finance/bexio/import_employees.py --dry-run` and `--apply`: 9 Employees on live
   (3 Active, 6 Left), rerun 0 new, nobody without a joining date (`private/payroll/needs-dates.txt`
   says 0). Counts only here; the importer prints counts only.
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
   **Stays manual:** the employer rates (AHV, ALV, UVG, KTG, FAK: the insurers' sheets — the KTG contract is read and
   its values are private, the settings follow in a follow-up bead, erp-1jh5; KTG is printed on no paystub), and the
   BVG plan (the band rates in the settings, then the insured salaries with them). The QST
   Tariff rows are loaded by `bi_payroll.swiss_payroll.load_tariff` from the ESTV file before the codes resolve; the
   Employee's code is picked from the QST Tariff Code list of its canton (erp-wvbn), and the loader makes those codes.
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
- **Lohnausweis is unchecked.** The erpnextswiss doctype was written for its own sample
  companies. Its field mapping is a config, not proof.
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
Form 11 lines. The line numbers and labels are the fields of erpnextswiss's Salary Certificate. Not yet
checked against the 2026 form (step 6 of the plan).

| Form line | Salary Component(s) on the slip | Column in view `Lohnausweis` | Status |
|---|---|---|---|
| 1. Lohn / Rente | Basic Salary | `line_1` | in payroll |
| 2.1 Verpflegung und Unterkunft | none | — | not in payroll |
| 2.2 Privatanteil Geschäftsfahrzeug | none | — | not in payroll |
| 2.3 Weitere Gehaltsnebenleistungen | none | — | not in payroll |
| 3. Unregelmässige Leistungen | none | — | not in payroll |
| 4. Kapitalleistungen | none | — | not in payroll |
| 5. Beteiligungsrechte | none | — | not in payroll |
| 6. Verwaltungsratsentschädigungen | none | — | not in payroll |
| 7. Andere Leistungen | none | — | not in payroll |
| 8. Bruttolohn total | gross pay of the slip | `line_8` | in payroll |
| 9. Beiträge AHV/IV/EO/ALV/NBUV | AHV/IV/EO Employee, ALV Employee, NBU Employee | `line_9` | in payroll |
| 10.1 Ordentliche Beiträge berufliche Vorsorge | BVG Employee | `line_10_1` | in payroll |
| 10.2 Beiträge Einkauf berufliche Vorsorge | none | — | not in payroll |
| 11. Nettolohn / Rente | net pay of the slip | `line_11` | in payroll |
| 12. Quellensteuerabzug | Quellensteuer Employee | `line_12` | in payroll since erp-6rrh (the tax is set on the slip from the gross, per canton tariff) |
| 13.1.1 Effektive Spesen Reise / Verpflegung / Übernachtung | none | — | not in payroll |
| 13.1.2 Effektive Spesen übrige | none | — | not in payroll |
| 13.2.1 Pauschalspesen Repräsentation | none | — | not in payroll |
| 13.2.2 Pauschalspesen Auto | none | — | not in payroll |
| 13.2.3 Pauschalspesen übrige | none | — | not in payroll |
| 13.3 Beiträge an die Weiterbildung | none | — | not in payroll |
| 14. Weitere Gehaltsnebenleistungen | none | — | not in payroll |
| 15. Bemerkungen | none (free text) | — | not in payroll |
| (no line) | KTG Employee | `ktg_employee` | open: the trustee says where it goes |

Unentgeltliche Beförderung and Kantinenverpflegung (checkboxes on the form) are not in payroll either.
The employer shares (AHV, ALV, BVG, UVG, KTG, FAK) are not on the form's lines and stay in the
hand-over view only.

## Copy stack for the HR train

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
  patches the live site has not run. PDFs work on the copy because `safety()` sets `host_name` to
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
finance/scripts/copy-stack.sh up <tag>
```

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
