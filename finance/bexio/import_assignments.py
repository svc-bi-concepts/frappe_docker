"""Set the payroll inputs in ERPNext from the paystub figures: one Salary Structure Assignment per Active employee,
the Quellensteuer fields on Employee, and the employee rates in Payroll Swiss Settings. Repeatable: a second run on
the same figures creates nothing.

The figures are private/payroll/paystub-figures.csv (parse_paystubs.py), the employees private/bexio-payroll/
employees.json (the export), the Employees are those import_employees.py wrote (keyed by bexio_id).

- The assignment's base is the monthly salary (1000 or 1001, as parse_paystubs reads them: 1001 up to 2025, 1000
  from 2025 on) of the employee's latest paystub month, from that month's first day, on the company's "Swiss Monthly"
  structure. Only Active Employees get one; Left ones keep their figures for the history (ledgerdemain, 2026-10-11).
- swiss_bvg_insured_salary is a calibration, not the fund's figure (ledgerdemain, 2026-10-11): worked back from the
  BVG employee contribution (5050) through the age band's employee rate in Payroll Swiss Settings, so the BVG Employee
  formula reproduces the latest paystub's amount. It inverts that formula (fixtures/salary_component.json): the monthly
  amount is (insured - coordination) / 12 * rate / 100, so insured = coordination + 1200 * amount / rate, rounded to the
  franc. The band rates are placeholders until the fund's sheet; the value is then replaced with the fund's figure. It is
  left empty and reported only when no band applies or the settings hold no rate for it. Each one is recorded in
  private/payroll/bvg-calibration.txt.
- Quellensteuer, for employees with a 5060 line: swiss_qst_canton is the canton of the bexio address (it is checked
  against the Select's options; the postcode table of the Swiss Post is not used, because 169 postcodes lie on a
  cantonal border). swiss_qst_code is the tariff code of that canton, valid in the latest month with a 5060 line,
  whose bracket holds that month's gross and whose rate is the printed 5060 rate. Unique: set; else reported with the
  count of candidates. The QST Tariff rows must be loaded first (bi_payroll.swiss_payroll.load_tariff).
- The employee rates 5010 (AHV), 5020 (ALV) and 5040 (NBU) are read from the Active employees' latest paystubs. An empty
  setting is filled when those agree on one rate; they disagreeing, the setting stays empty and the rates with the ids
  are reported (the one-rate model cannot carry two). A different setting is reported, never overwritten. Employer
  rates, KTG and the BVG plan are not on a paystub: reported as needing the insurer's or the fund's sheet.

PRIVACY: payroll data is the company's most sensitive data and this repo is public. The importer reads private/ by
absolute path and prints counts only. Per-employee problems go to private/payroll/assignment-problems.txt (mode 600),
the stderr lines name the record position only. Tests use invented employees and figures.

    python3 finance/bexio/import_assignments.py            # dry run, the default
    python3 finance/bexio/import_assignments.py --apply    # writes assignments, Employee fields and settings

Runs as the API user through the REST API (import_master.Erp). Standard library only.
"""

import argparse
import collections
import csv
import datetime
import os
import sys

from import_employees import PAYROLL_DIR, load_payroll, month_end, month_start
from import_master import COMPANY, Erp, ErpError, PRIVATE, TOKEN_FILE, differs

FIGURES_CSV = os.path.join(PRIVATE, "payroll", "paystub-figures.csv")
PROBLEMS_TXT = os.path.join(PRIVATE, "payroll", "assignment-problems.txt")
CALIBRATION_TXT = os.path.join(PRIVATE, "payroll", "bvg-calibration.txt")

SETTINGS = "Payroll Swiss Settings"
STRUCTURE_PREFIX = "Swiss Monthly"
STATUS_ACTIVE = "Active"
# Employee's swiss_qst_canton Select options (fixtures/custom_field.json)
CANTONS = {
    "AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR", "JU", "LU", "NE", "NW", "OW", "SG", "SH", "SZ",
    "SO", "TG", "TI", "UR", "VD", "VS", "ZG", "ZH",
}

# the monthly salary: 1001 up to 2025, 1000 from 2025 on (parse_paystubs.SALARY_TYPES); a paystub prints one of them
SALARY_TYPES = ("1001", "1000")
GROSS_TYPE = "5000"
BVG_TYPE = "5050"
QST_TYPE = "5060"
# paystub wage type -> Payroll Swiss Settings' employee rate (percent)
PAYSTUB_RATES = [("5010", "swiss_ahv_ee"), ("5020", "swiss_alv_ee"), ("5040", "swiss_nbu_ee")]
# not on a paystub (KTG is printed on none of them): reported per field, to be filled from the insurer's sheet
INSURER_FIELDS = ["swiss_ahv_ag", "swiss_alv_ag", "swiss_uvg_ag", "swiss_ktg_ee", "swiss_ktg_ag", "swiss_fak_ag"]
# the BVG band rates (age 25 to 64) of Payroll Swiss Settings: (from age, to age, field), the formula's bands
BVG_BANDS = [(25, 35, "swiss_bvg_ee_25"), (35, 45, "swiss_bvg_ee_35"), (45, 55, "swiss_bvg_ee_45"), (55, 65, "swiss_bvg_ee_55")]
BVG_FIELDS = [field for _low, _high, field in BVG_BANDS] + ["swiss_bvg_ag_25", "swiss_bvg_ag_35", "swiss_bvg_ag_45", "swiss_bvg_ag_55"]
RATE_TOLERANCE = 0.005  # percent: the printed rates have two decimals at most


# ---- the figures ----

def _number(text):
    return float(text) if text else None


def read_figures(path=FIGURES_CSV):
    """{bexio id: [row]} of the figures CSV, with the figures as numbers (None where the CSV is empty)."""
    rows = {}
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = {
                "year": int(raw["year"]), "month": int(raw["month"]), "wage_type": raw["wage_type"],
                "base": _number(raw["base"]), "rate": _number(raw["rate"]), "amount": _number(raw["amount"]),
            }
            rows.setdefault(raw["employee_id"], []).append(row)
    return rows


def months(rows):
    return sorted({(row["year"], row["month"]) for row in rows})


def line(rows, month, wage_type):
    """The figures line of one wage type in one paystub month (one per type), or None."""
    return next((row for row in rows if (row["year"], row["month"]) == month and row["wage_type"] == wage_type), None)


def latest_line(rows, wage_type):
    """The line of the wage type in the most recent month that prints it, or None."""
    lines = [row for row in rows if row["wage_type"] == wage_type]
    return max(lines, key=lambda row: (row["year"], row["month"])) if lines else None


# ---- the rules ----

def age_on(born, on):
    """Whole years from the date of birth to the date (both datetime.date)."""
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def bvg_band_field(age):
    """The Payroll Swiss Settings field of the BVG employee rate for the age, or None outside 25 to 64."""
    for low, high, field in BVG_BANDS:
        if low <= age < high:
            return field
    return None


def insured_salary(monthly, rate, coordination):
    """The yearly insured salary whose monthly BVG Employee amount is the given one at the rate, to the franc."""
    return round(coordination + 1200.0 * monthly / rate)


def qst_codes(tariffs, canton, gross, rate, on):
    """The tariff codes of the canton that hold the gross's bracket on the date at the rate, sorted. Mirrors
    quellensteuer.find_tariff: the rows valid on the date and holding the gross, the latest valid_from per code.
    tariffs: the QST Tariff rows of the canton as the API gives them (dates as ISO text)."""
    latest = {}
    for row in tariffs:
        if row["canton"] != canton or row["valid_from"] > on or not row["income_from"] <= gross < row["income_from"] + row["step"]:
            continue
        best = latest.get(row["tariff_code"])
        if best is None or row["valid_from"] > best["valid_from"]:
            latest[row["tariff_code"]] = row
    return sorted(code for code, row in latest.items() if abs(row["rate"] - rate) < RATE_TOLERANCE)


# ---- the plan ----

class Plan:
    """What the figures give, per record, and the counts for the report. Nothing is written here."""

    def __init__(self):
        self.entries = []
        self.counts = collections.Counter()
        self.problems = []  # (position, text): stderr gets the position, the file the name too
        self.calibrations = []  # (employee name, paystub month, insured salary): the BVG calibration file

    def add(self, position, name, text):
        """A problem of a record (position, name), or of the settings when position is None."""
        self.counts[text.split(":")[0]] += 1
        self.problems.append((position, name, text))

    @staticmethod
    def where(position):
        return "settings" if position is None else "record {}".format(position)


def build_plan(employees, figures, erp_employees, settings, structure, tariffs_for, existing):
    """The entries of the run: per record, the Employee, the assignment, the Employee fields and the problems.

    employees: the export's records; figures: read_figures(); erp_employees: {bexio id: Employee row} from ERPNext;
    settings: Payroll Swiss Settings as ERPNext gives it; tariffs_for(canton) -> the QST Tariff rows of the canton;
    existing: {(employee name, from date): docstatus} of the Salary Structure Assignments already made.
    """
    plan = Plan()
    coordination = settings.get("swiss_bvg_coordination") or 0
    for position, emp in enumerate(employees, start=1):
        bexio_id = str(emp["id"])
        rows = figures.get(bexio_id) or []
        erp_row = erp_employees.get(bexio_id)
        if not rows:
            plan.counts["no paystubs"] += 1
            continue
        if erp_row is None:
            plan.add(position, "", "no Employee for the bexio id: run import_employees.py first")
            continue
        name = erp_row["name"]
        latest = max(months(rows))
        entry = {"position": position, "bexio_id": bexio_id, "name": name, "fields": {}, "assignment": None}
        plan.entries.append(entry)
        plan.counts["records with paystubs"] += 1
        if erp_row["status"] == STATUS_ACTIVE:
            _assignment(plan, entry, rows, latest, emp, settings, coordination, structure, existing)
        else:
            plan.counts["left, no assignment"] += 1
        _quellensteuer(plan, entry, rows, latest, emp, tariffs_for)
    return plan


def _assignment(plan, entry, rows, latest, emp, settings, coordination, structure, existing):
    from_date = month_start(latest)
    if (entry["name"], from_date) in existing:
        if existing[(entry["name"], from_date)] == 0:
            plan.add(entry["position"], entry["name"], "assignment exists as a draft: submit it by hand")
        else:
            plan.counts["assignments already made"] += 1
        return
    salaries = [line(rows, latest, wage_type) for wage_type in SALARY_TYPES]
    salary = next((row for row in salaries if row is not None), None)
    if salary is None or salary["amount"] is None:
        plan.add(entry["position"], entry["name"], "no monthly salary (1000/1001) in the latest paystub month")
        return
    assignment = {
        "employee": entry["name"], "salary_structure": structure, "from_date": from_date,
        "base": salary["amount"], "company": COMPANY,
    }
    bvg = line(rows, latest, BVG_TYPE)
    insured = None
    if bvg is not None:
        born = datetime.date.fromisoformat(emp["date_of_birth"])
        field = bvg_band_field(age_on(born, datetime.date.fromisoformat(month_end(latest))))
        rate = settings.get(field) if field else None
        if field is None:
            plan.add(entry["position"], entry["name"], "BVG insured salary unresolved: no BVG band at this age")
        elif not rate or not coordination:
            plan.add(entry["position"], entry["name"], "BVG insured salary unresolved: no band rate or coordination in the settings")
        else:
            value = insured_salary(abs(bvg["amount"]), rate, coordination)
            if value < (settings.get("swiss_bvg_threshold") or 0):
                # the BVG formulas give 0 below the entry threshold, so this value would not reproduce the paystub
                plan.add(entry["position"], entry["name"], "BVG insured salary unresolved: below the entry threshold")
            else:
                insured = value
                assignment["swiss_bvg_insured_salary"] = insured
                plan.calibrations.append((entry["name"], "{:04d}-{:02d}".format(*latest), insured))
    if insured is not None:
        plan.counts["BVG insured salary calibrated"] += 1
    entry["assignment"] = assignment
    plan.counts["assignments to make"] += 1


def _quellensteuer(plan, entry, rows, latest, emp, tariffs_for):
    withholding = [row for row in rows if row["wage_type"] == QST_TYPE]
    if not withholding:
        plan.counts["no QST (no 5060)"] += 1
        return
    canton = (emp.get("address") or {}).get("canton") or ""
    if canton not in CANTONS:
        plan.add(entry["position"], entry["name"], "Quellensteuer canton unresolved: the bexio address has none")
        return
    entry["fields"]["swiss_qst_canton"] = canton
    plan.counts["employees with QST"] += 1
    month = max((row["year"], row["month"]) for row in withholding)
    qst = line(rows, month, QST_TYPE)
    gross = line(rows, month, GROSS_TYPE)
    if qst is None or gross is None or qst["rate"] is None or gross["amount"] is None:
        plan.add(entry["position"], entry["name"], "Quellensteuer code unresolved: the 5060 line prints no rate or no gross")
        return
    codes = qst_codes(tariffs_for(canton), canton, gross["amount"], qst["rate"], month_end(month))
    if len(codes) == 1:
        entry["fields"]["swiss_qst_code"] = codes[0]
        plan.counts["codes resolved"] += 1
    else:
        reason = "no tariff code" if not codes else f"{len(codes)} candidate codes"
        plan.add(entry["position"], entry["name"], f"Quellensteuer code unresolved: {reason}")


def rate_report(plan, figures, settings, active):
    """Per paystub rate: the settings to fill (empty ones) and the differences; the employer rates are only listed.
    The rates are read from the Active employees' latest paystubs (active: their bexio ids); a Left employee's
    paystub does not decide the setting."""
    fills = {}
    for wage_type, field in PAYSTUB_RATES:
        found = [(bexio_id, latest_line(figures[bexio_id], wage_type)) for bexio_id in sorted(active) if bexio_id in figures]
        found = [(bexio_id, row) for bexio_id, row in found if row is not None and row["rate"] is not None]
        if not found:
            plan.add(None, "", f"rate {field}: not on any active employee's paystub")
            continue
        by_rate = collections.defaultdict(list)
        for bexio_id, row in found:
            by_rate[row["rate"]].append(bexio_id)
        if len(by_rate) != 1:
            detail = "; ".join("{} for {}".format(rate, ", ".join(ids)) for rate, ids in sorted(by_rate.items()))
            plan.add(None, "", f"rate {field}: the active employees' latest paystubs print {len(by_rate)} rates, "
                               f"not filled: {detail}")
            continue
        rate = next(iter(by_rate))
        have = settings.get(field)
        if not have:
            fills[field] = rate
            plan.counts["rates to fill"] += 1
        elif abs(have - rate) >= RATE_TOLERANCE:
            plan.add(None, "", f"rate {field}: the setting differs from the paystubs, not changed")
        else:
            plan.counts["rates equal"] += 1
    plan.counts["rates needing insurer sheet"] = len(INSURER_FIELDS)
    plan.counts["BVG fields needing fund sheet"] = len(BVG_FIELDS)
    return fills


# ---- the writes ----

class Writer:
    """The writes of --apply. Each one is refused by the API or counted; nothing is written in a dry run."""

    def __init__(self, erp, dry_run):
        self.erp = erp
        self.dry_run = dry_run
        self.counts = collections.Counter()
        self.failures = []

    def run(self, plan, fills, erp_employees):
        for entry in plan.entries:
            if entry["fields"]:
                self.employee_fields(entry, erp_employees)
            if entry["assignment"]:
                self.assignment(entry)
        if fills:
            self.settings(fills)
        return self.counts

    def employee_fields(self, entry, erp_employees):
        current = {k: v for k, v in erp_employees[entry["bexio_id"]].items() if k in entry["fields"]}
        if not differs(current, entry["fields"]):
            self.counts["Employee fields unchanged"] += 1
            return
        self._write(entry, lambda: self.erp.update("Employee", entry["name"], entry["fields"]), "Employee fields set")

    def assignment(self, entry):
        def write():
            doc = self.erp.insert("Salary Structure Assignment", entry["assignment"])
            self.erp.call("frappe.client.submit", doc=doc)
        self._write(entry, write, "assignments made")

    def settings(self, fills):
        def write():
            self.erp.call("frappe.client.set_value", doctype=SETTINGS, name=SETTINGS, fieldname=fills)
        if self.dry_run:
            self.counts["settings fields to fill"] += len(fills)
            return
        try:
            write()
            self.counts["settings fields filled"] += len(fills)
        except ErpError as err:
            self.failures.append(f"settings: write refused ({type(err).__name__})")

    def _write(self, entry, write, what):
        if self.dry_run:
            self.counts[what + " (dry run)"] += 1
            return
        try:
            write()
            self.counts[what] += 1
        except ErpError as err:
            self.failures.append(f"record {entry['position']}: write refused ({type(err).__name__})")


# ---- the ERPNext side ----

def erp_employees_by_bexio_id(erp):
    rows = erp.list("Employee", filters=[["bexio_id", "is", "set"]],
                    fields=["name", "bexio_id", "status", "swiss_qst_canton", "swiss_qst_code"])
    return {row["bexio_id"]: row for row in rows}


def structure_name(erp):
    rows = erp.list("Salary Structure", filters=[["company", "=", COMPANY], ["docstatus", "=", 1]])
    names = [row["name"] for row in rows if row["name"].startswith(STRUCTURE_PREFIX)]
    if len(names) != 1:
        raise ErpError("structure", "found {} submitted {} structures".format(len(names), STRUCTURE_PREFIX))
    return names[0]


def existing_assignments(erp):
    rows = erp.list("Salary Structure Assignment", filters=[["docstatus", "<", 2]], fields=["employee", "from_date", "docstatus"])
    return {(row["employee"], row["from_date"]): row["docstatus"] for row in rows}


def tariff_reader(erp):
    cache = {}

    def tariffs_for(canton):
        if canton not in cache:
            cache[canton] = erp.list("QST Tariff", filters=[["canton", "=", canton]], fields=[
                "canton", "tariff_code", "valid_from", "income_from", "step", "rate"])
        return cache[canton]
    return tariffs_for


def write_private(path, lines):
    """A file of private figures: mode 600, replaced on each run."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_problems(path, plan):
    lines = ["# assignment problems: {}".format(len(plan.problems))]
    for position, name, text in plan.problems:
        lines.append("{}  {}  {}".format(Plan.where(position), name, text).rstrip())
    write_private(path, lines)


def write_calibration(path, plan):
    """The BVG insured salaries worked back from the paystubs with the placeholder band rates: until the fund's sheet
    replaces them, they are a calibration of the formula, not the fund's figure. A run that calibrates none (the rerun
    after --apply) leaves the file of the run that did."""
    if not plan.calibrations:
        return
    lines = ["# BVG insured salary calibrated from the paystub 5050 at the settings' band rate: {}".format(
        len(plan.calibrations)), "# employee  paystub month  insured salary"]
    for name, month, insured in plan.calibrations:
        lines.append("{}  {}  {}".format(name, month, insured))
    write_private(path, lines)


def report(plan, writer, fills, dry_run):
    for label, count in sorted(plan.counts.items()):
        print("{}: {}".format(label, count))
    for label, count in sorted(writer.counts.items()):
        print("{}: {}".format(label, count))
    print("settings to fill: {}".format(len(fills)))
    print("insurer sheet fields: " + ", ".join(INSURER_FIELDS))
    print("dry run: nothing was written" if dry_run else "written to ERPNext")
    print("problems list: {}".format(PROBLEMS_TXT))
    print("BVG calibration list: {}".format(CALIBRATION_TXT))


def main(argv):
    parser = argparse.ArgumentParser(description="Set the payroll assignments, Quellensteuer fields and rates in ERPNext.")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing but the problems list (default)")
    parser.add_argument("--apply", action="store_true", help="create the assignments, set the Employee fields and the settings")
    parser.add_argument("--token-file", default=TOKEN_FILE, help="API keys file (default: the master importer's)")
    args = parser.parse_args(argv)
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")
    dry_run = not args.apply

    employees, _paystubs = load_payroll(PAYROLL_DIR)
    figures = read_figures()
    erp = Erp.from_file(args.token_file)
    try:
        erp_employees = erp_employees_by_bexio_id(erp)
        structure = structure_name(erp)
        settings = erp.call("frappe.client.get", doctype=SETTINGS, name=SETTINGS)
        existing = existing_assignments(erp)
    except ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    plan = build_plan(employees, figures, erp_employees, settings, structure, tariff_reader(erp), existing)
    active = {bexio_id for bexio_id, row in erp_employees.items() if row["status"] == STATUS_ACTIVE}
    fills = rate_report(plan, figures, settings, active)
    writer = Writer(erp, dry_run)
    writer.run(plan, fills, erp_employees)
    write_problems(PROBLEMS_TXT, plan)
    write_calibration(CALIBRATION_TXT, plan)
    report(plan, writer, fills, dry_run)
    for position, _name, text in plan.problems:
        print("  problem: {}: {}".format(Plan.where(position), text), file=sys.stderr)
    for text in writer.failures:
        print("  problem: " + text, file=sys.stderr)
    return 1 if writer.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
