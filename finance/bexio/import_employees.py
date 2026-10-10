"""Import the exported bexio payroll employees into ERPNext Employee, repeatable.

One Employee per record of private/bexio-payroll/employees.json, keyed by the
bexio id in the custom field bexio_id (swiss-setup.sh fields): looked up there,
updated when a field differs, created when missing, never matched by name. A
second run on the same export creates nothing.

The joining and leaving dates come from the paystub months (paystubs.json):
the first month gives date_of_joining as its first day, and a last month
before the export's last month means the employee left on that month's last
day (status Left, relieving_date). private/payroll/employee-dates.csv
(header row: bexio_id, joined, left; dates YYYY-MM-DD, left empty for the
still employed) overrides these and dates the employees without paystubs.
A record with no joining date at all is reported as needing one and is not
created; the list goes to private/payroll/needs-dates.txt (mode 600) for the
operator.

PRIVACY: payroll data is the company's most sensitive data and this repo is
public. The importer reads private/ by absolute path and prints counts only;
the needs-dates list is its only written output and stays in private/ (mode
600). Tests use invented employees.

The AHV number goes to erpnextswiss's social_security_number; nationality,
stay permit category and language have no Employee field and go into the bio.
Salaries, BVG, Quellensteuer and rates are other beads' work (paystub parser,
assignments), not this import. hrms.md plan step 4.

Runs as api-agent@finance.local (HR User role) through the REST API; the key
and secret are read from the token file and never printed:

    python3 finance/bexio/import_employees.py            # dry run, the default
    python3 finance/bexio/import_employees.py --apply    # writes Employees

Standard library only.
"""

import argparse
import calendar
import csv
import json
import os
import sys

from import_master import COMPANY, Erp, ErpError, PRIVATE, TOKEN_FILE, differs

PAYROLL_DIR = os.path.join(PRIVATE, "bexio-payroll")
DATES_CSV = os.path.join(PRIVATE, "payroll", "employee-dates.csv")
NEEDS_DATES = os.path.join(PRIVATE, "payroll", "needs-dates.txt")

# Employee autonames by naming series; the field has no default, so the importer sets it.
NAMING_SERIES = "HR-EMP-"
# bexio employment_level -> ERPNext Employment Type: 100 = Full-time, else Part-time.
FULL_TIME_LEVEL = 100
FULL_TIME, PART_TIME = "Full-time", "Part-time"
# Employee's marital_status Select options (live site, v16); anything else is left empty.
MARITAL_OPTIONS = ("Single", "Married", "Divorced", "Widowed")
STATUS_ACTIVE, STATUS_LEFT = "Active", "Left"
SWISS_COUNTRY = "CH"

# Employee field <- bexio employees.json key, for the dry run's fill counts.
FIELD_SOURCES = [
    ("first_name", "first_name"), ("last_name", "last_name"), ("gender", "gender"),
    ("date_of_birth", "date_of_birth"), ("marital_status", "marital_status"),
    ("cell_number", "phone_number"), ("personal_email", "email"),
    ("current_address", "address"), ("iban", "iban"),
    ("social_security_number", "ahv_number"), ("employment_type", "employment_level"),
]


# ---- the export ----

def load_payroll(path):
    """(employees, paystubs) of the payroll export (export.py --payroll)."""
    rows = {}
    for name in ("employees", "paystubs"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            rows[name] = json.load(f)
    return rows["employees"], rows["paystubs"]


def read_dates_csv(path=DATES_CSV):
    """{bexio id: row} from the operator's CSV, or {} when the file is not there.

    Header row with the columns bexio_id, joined, left ('bexio id' is accepted
    too); dates YYYY-MM-DD, left empty for the still employed. Extra columns
    are kept but unused.
    """
    if not os.path.exists(path):
        return {}
    rows = {}
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in raw.items()}
            if row.get("bexio_id"):
                rows[row["bexio_id"]] = row
    return rows


# ---- the mapping ----

def month_key(stub):
    return (int(stub["year"]), int(stub["month"]))


def month_start(month):
    return "{:04d}-{:02d}-01".format(*month)


def month_end(month):
    return "{:04d}-{:02d}-{:02d}".format(month[0], month[1], calendar.monthrange(month[0], month[1])[1])


def marital_status(value):
    """bexio's lowercase word onto Employee's Select options; empty when not one of them (reported, not guessed)."""
    return {option.lower(): option for option in MARITAL_OPTIONS}.get(str(value or "").lower(), "")


def composed_address(addr):
    """The bexio address dict as the text for current_address; '' when nothing is usable.

    Street line (street_name + house_number, or street, which often carries the
    number already), the extra lines, zip and city, and the country only when
    it is not Switzerland. Canton and municipality id stay out: the zip says it.
    """
    if not isinstance(addr, dict):
        return ""
    street = " ".join(part for part in (addr.get("street_name"), addr.get("house_number")) if part) or addr.get("street") or ""
    city = addr.get("locality") if addr.get("locality") and addr.get("locality") != addr.get("city") else addr.get("city")
    lines = [street, addr.get("complementary_line") or "", addr.get("postbox") or "",
             " ".join(part for part in (addr.get("zip_code"), city) if part)]
    country = str(addr.get("country") or "").upper()
    if country and country != SWISS_COUNTRY:
        lines.append(country)
    return "\n".join(line for line in lines if line)


def bio_text(emp):
    """Nationality, stay permit and language have no Employee field; they go into the bio."""
    rows = [("Nationality", emp.get("nationality")), ("Stay permit", emp.get("stay_permit_category")),
            ("Language", emp.get("language"))]
    return "\n".join("{}: {}".format(label, value) for label, value in rows if value)


def dates_for(months, last_month, override=None):
    """(joined, left) from the paystub months, the CSV override first; None when no date is known.

    joined is the first day of the first paystub month; a last month before the
    export's last month means Left on that month's last day. The override row
    from employee-dates.csv wins when it carries a joined date; its left (also
    empty = still employed) wins over the derived one. An employee without
    paystubs and without a joined date returns None.
    """
    override = override or {}
    joined = (override.get("joined") or "").strip()
    left = (override.get("left") or "").strip()
    if joined:
        return joined, left
    if not months or last_month is None:
        return None
    first, last = min(months), max(months)
    if not left and last < last_month:
        left = month_end(last)
    return month_start(first), left


def employee_doc(emp, gender, joined, left):
    """The Employee fields the export fills; gender and dates come from the caller."""
    doc = {
        "naming_series": NAMING_SERIES,
        "first_name": emp.get("first_name") or "",
        "last_name": emp.get("last_name") or "",
        "gender": gender,
        "date_of_birth": emp.get("date_of_birth") or "",
        "marital_status": marital_status(emp.get("marital_status")),
        "cell_number": emp.get("phone_number") or "",
        "personal_email": emp.get("email") or "",
        "current_address": composed_address(emp.get("address")),
        "iban": emp.get("iban") or "",
        "social_security_number": emp.get("ahv_number") or "",
        "company": COMPANY,
        "employment_type": FULL_TIME if emp.get("employment_level") == FULL_TIME_LEVEL else PART_TIME,
        "bio": bio_text(emp),
        "date_of_joining": joined,
        "status": STATUS_ACTIVE,
    }
    if left:
        doc["relieving_date"] = left
        doc["status"] = STATUS_LEFT
    return doc


def build_plan(employees, paystubs, overrides, gender_names):
    """[(employee, doc, reason)]: a doc of None marks a record that is not imported, with the reason.

    The export's last month is the newest paystub month of the whole export;
    the Gender names are the site's, matched without case, so an unknown value
    is reported instead of guessed.
    """
    months = {}
    for stub in paystubs:
        months.setdefault(str(stub["employee_id"]), []).append(month_key(stub))
    last_month = max((month_key(s) for s in paystubs), default=None)
    gender_of = {g.lower(): g for g in gender_names}
    plan = []
    for emp in employees:
        gender = gender_of.get(str(emp.get("gender") or "").lower())
        if not gender:
            plan.append((emp, None, "unknown gender: {}".format(emp.get("gender"))))
            continue
        dates = dates_for(months.get(str(emp["id"])), last_month, overrides.get(str(emp["id"])))
        if dates is None:
            plan.append((emp, None, "needs joining date"))
            continue
        plan.append((emp, employee_doc(emp, gender, *dates), ""))
    return plan


# ---- the upsert ----

class Counts:
    def __init__(self):
        self.created = self.updated = self.unchanged = self.failed = 0
        self.existing = 0

    def line(self):
        return "to create: {}  already there: {}  updated: {}  unchanged: {}  failed: {}".format(
            self.created, self.existing, self.updated, self.unchanged, self.failed)


class EmployeeImporter:
    """The upsert keyed by bexio_id, the counts and the problems; no names or numbers in either."""

    def __init__(self, erp, dry_run=True):
        self.erp = erp
        self.dry_run = dry_run
        self.counts = Counts()
        self.counts.existing = 0
        self.problems = []
        self.needs_dates = []
        self._index = None

    def index(self):
        if self._index is None:
            rows = self.erp.list("Employee", [["bexio_id", "is", "set"]], ["name", "bexio_id"])
            self._index = {r["bexio_id"]: r["name"] for r in rows}
        return self._index

    def run(self, plan):
        ids = {str(emp["id"]) for emp, _doc, _reason in plan}
        self.counts.existing = len(set(self.index()) & ids)
        for emp, doc, reason in plan:
            if doc is None:
                if reason == "needs joining date":
                    self.needs_dates.append(emp)
                else:
                    self.counts.failed += 1
                    self.problems.append("employee {}: {}".format(emp["id"], reason))
                continue
            self.upsert(str(emp["id"]), doc)
        return self.counts

    def upsert(self, bexio_id, doc):
        doc = dict(doc, bexio_id=bexio_id)
        known = self.index()
        try:
            if bexio_id in known:
                name = known[bexio_id]
                # the naming series only matters on create; an existing Employee keeps its series
                want = {k: v for k, v in doc.items() if k != "naming_series"}
                if differs(self.erp.get("Employee", name), want):
                    if not self.dry_run:
                        self.erp.update("Employee", name, want)
                    self.counts.updated += 1
                else:
                    self.counts.unchanged += 1
            else:
                if not self.dry_run:
                    self.erp.insert("Employee", doc)
                self.counts.created += 1
        except ErpError as err:
            self.counts.failed += 1
            self.problems.append("employee {}: {}".format(bexio_id, err))


# ---- output ----

def fill_counts(employees):
    """Per mapped field, how many records carry it (counts only, no values)."""
    return [(label, sum(1 for emp in employees if emp.get(key))) for label, key in FIELD_SOURCES]


def write_needs_dates(path, employees):
    """The list for the operator: who has no joining date. Counts in the first line, mode 600."""
    lines = ["# employees without a joining date: {}".format(len(employees))]
    for emp in employees:
        lines.append("{} {}  bexio id {}  personal nr {}".format(
            emp.get("first_name") or "", emp.get("last_name") or "", emp.get("id"), emp.get("personal_number") or ""))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def report(importer, plan, dry_run):
    employees = [emp for emp, _doc, _reason in plan]
    status = {"Active": 0, "Left": 0}
    for _emp, doc, _reason in plan:
        if doc:
            status[doc["status"]] += 1
    print("records: {}".format(len(plan)))
    print(importer.counts.line())
    print("fields filled: " + ", ".join("{}/{}".format(label, count) for label, count in fill_counts(employees)))
    unknown = sum(1 for _emp, doc, reason in plan if doc is None and reason.startswith("unknown gender"))
    print("unknown gender: {}  needs joining date: {}".format(unknown, len(importer.needs_dates)))
    print("status: Active {}, Left {}".format(status["Active"], status["Left"]))
    print("dry run: nothing was written" if dry_run else "written to ERPNext")
    print("needs-dates list: {}".format(NEEDS_DATES))


def main(argv):
    parser = argparse.ArgumentParser(description="Import the bexio payroll employees into ERPNext Employee.")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing but the needs-dates list (default)")
    parser.add_argument("--apply", action="store_true", help="create and update the Employees through the API user")
    parser.add_argument("--token-file", default=TOKEN_FILE, help="API keys file (default: the master importer's)")
    args = parser.parse_args(argv)
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")
    dry_run = not args.apply

    employees, paystubs = load_payroll(PAYROLL_DIR)
    overrides = read_dates_csv()
    erp = Erp.from_file(args.token_file)
    try:
        genders = [row["name"] for row in erp.list("Gender")]
    except ErpError as err:
        print("aborted: the Gender list is not readable: {}".format(err), file=sys.stderr)
        return 2
    importer = EmployeeImporter(erp, dry_run=dry_run)
    try:
        plan = build_plan(employees, paystubs, overrides, genders)
        counts = importer.run(plan)
    except ErpError as err:
        # the lookup every record depends on failed (the bexio_id field is
        # missing: run finance/scripts/swiss-setup.sh fields first)
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    write_needs_dates(NEEDS_DATES, importer.needs_dates)
    report(importer, plan, dry_run)
    for line in importer.problems:
        print("  problem: " + line, file=sys.stderr)
    return 1 if counts.failed or importer.problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
