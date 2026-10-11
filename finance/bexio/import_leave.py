"""Set up the 2026 Swiss SZ holiday and Ferien allocation in HRMS.

Dry run is the default. Apply is intended for the later go-live bead only;
this child bead must prove its behavior on a copy, not write to the live site.
Payroll data stays private and output contains counts and record positions only.

The canton-wide public holidays are from the Kanton Schwyz's 2026 list:
https://www.sz.ch/public/upload/assets/71606/Kantonale_lokale_und_nationale_Feiertage.pdf
Municipal holidays and administration closures are excluded.
"""

import argparse
import datetime
import json
import os
import stat
import sys

from import_employees import load_payroll
from import_master import COMPANY, Erp, ErpError, PRIVATE, TOKEN_FILE, differs

YEAR = 2026
FROM_DATE = "2026-01-01"
TO_DATE = "2026-12-31"
HOLIDAY_LIST = "Schweiz SZ 2026"
LEAVE_TYPE = "Ferien"
LEAVE_PERIOD = (FROM_DATE, TO_DATE)
DEFAULTS_FILE = os.path.join(PRIVATE, "payroll", "leave-defaults.txt")

# Canton-wide dates only. Source: Kanton Schwyz, Feiertage 2026 (see docstring).
PUBLIC_HOLIDAYS = [
    ("2026-01-01", "Neujahr"),
    ("2026-01-06", "Heilige Drei Könige"),
    ("2026-03-19", "Josefstag"),
    ("2026-04-03", "Karfreitag"),
    ("2026-04-06", "Ostermontag"),
    ("2026-05-14", "Auffahrt"),
    ("2026-05-25", "Pfingstmontag"),
    ("2026-06-04", "Fronleichnam"),
    ("2026-08-01", "Nationalfeiertag"),
    ("2026-08-15", "Mariä Himmelfahrt"),
    ("2026-12-08", "Mariä Empfängnis"),
    ("2026-12-25", "Weihnachten"),
    ("2026-12-26", "Stephanstag"),
]


def holiday_rows():
    """Rows for public holidays and the standard Saturday/Sunday work week."""
    rows = {date: {"holiday_date": date, "description": name} for date, name in PUBLIC_HOLIDAYS}
    day = datetime.date(YEAR, 1, 1)
    last = datetime.date(YEAR, 12, 31)
    while day <= last:
        if day.weekday() >= 5:
            rows.setdefault(day.isoformat(), {
                "holiday_date": day.isoformat(), "description": "Saturday" if day.weekday() == 5 else "Sunday",
                "weekly_off": 1,
            })
        day += datetime.timedelta(days=1)
    return sorted(rows.values(), key=lambda row: row["holiday_date"])


def age_on(dob, on=FROM_DATE):
    if not dob:
        return None
    born = datetime.date.fromisoformat(str(dob)[:10])
    date = datetime.date.fromisoformat(on)
    return date.year - born.year - ((date.month, date.day) < (born.month, born.day))


def allocation_days(employee, export_employee):
    """(days, used_default): export amount wins; otherwise Swiss minimum pro rata."""
    exported = export_employee.get("annual_vacation_days_total")
    if exported not in (None, ""):
        return float(exported), False
    age = age_on(export_employee.get("date_of_birth") or employee.get("date_of_birth"))
    full_time_minimum = 25 if age is not None and age < 20 else 20
    level = employee.get("swiss_employment_level")
    if level in (None, ""):
        level = export_employee.get("employment_level")
    if level in (None, ""):
        # Without a known work percentage, do not assume full-time entitlement.
        raise ValueError("employment level missing")
    return full_time_minimum * float(level) / 100, True


def build_plan(export_employees, erp_employees):
    """Per-active-employee allocation plan keyed by the bexio id, without printing values."""
    plan = []
    defaults = []
    problems = []
    for position, export_emp in enumerate(export_employees, 1):
        bexio_id = str(export_emp["id"])
        employee = erp_employees.get(bexio_id)
        if not employee:
            problems.append((position, "Employee not found"))
            continue
        if employee.get("status") != "Active":
            continue
        try:
            days, is_default = allocation_days(employee, export_emp)
        except (TypeError, ValueError):
            problems.append((position, "default allocation needs employment level"))
            continue
        plan.append({"position": position, "employee": employee, "bexio_id": bexio_id, "days": days,
                     "default": is_default})
        if is_default:
            defaults.append((employee, days))
    return plan, defaults, problems


class LeaveImporter:
    def __init__(self, erp, dry_run=True):
        self.erp = erp
        self.dry_run = dry_run
        self.counts = {key: 0 for key in ("created", "updated", "unchanged", "failed", "skipped")}
        self.problems = []

    def upsert(self, doctype, key_filters, doc):
        rows = self.erp.list(doctype, key_filters, ["name"])
        if rows:
            name = rows[0]["name"]
            have = self.erp.get(doctype, name)
            if not differs(have, doc):
                self.counts["unchanged"] += 1
                return name
            if not self.dry_run:
                self.erp.update(doctype, name, doc)
            self.counts["updated"] += 1
            return name
        if not self.dry_run:
            result = self.erp.insert(doctype, doc)
            name = result["name"]
        else:
            name = None
        self.counts["created"] += 1
        return name

    def run_setup(self):
        self.upsert("Holiday List", [["holiday_list_name", "=", HOLIDAY_LIST]], {
            "holiday_list_name": HOLIDAY_LIST, "from_date": FROM_DATE, "to_date": TO_DATE,
            "country": "Switzerland", "subdivision": "Schwyz", "weekly_off": "",
            "holidays": holiday_rows(),
        })
        self.upsert("Leave Type", [["leave_type_name", "=", LEAVE_TYPE]], {
            "leave_type_name": LEAVE_TYPE, "is_carry_forward": 1, "is_earned_leave": 0,
        })
        self.upsert("Leave Period", [["from_date", "=", FROM_DATE], ["to_date", "=", TO_DATE],
                                      ["company", "=", COMPANY]], {
            "from_date": FROM_DATE, "to_date": TO_DATE, "company": COMPANY, "is_active": 1,
        })

    def run_employees(self, plan, holiday_list_name=None, leave_period_name=None):
        for entry in plan:
            employee = entry["employee"]
            # HRMS 16 retains Employee.holiday_list. Leave List Assignment can be a follow-up
            # if it is enabled on a site; the Company default remains the source of new records.
            employee_patch = {"holiday_list": holiday_list_name or HOLIDAY_LIST}
            if differs(employee, employee_patch):
                if not self.dry_run:
                    try:
                        self.erp.update("Employee", employee["name"], employee_patch)
                        employee.update(employee_patch)
                    except ErpError as err:
                        self.counts["failed"] += 1
                        self.problems.append((entry["position"], "Employee holiday list refused ({})".format(type(err).__name__)))
                        continue
                else:
                    self.counts["updated"] += 1
            else:
                self.counts["unchanged"] += 1
            filters = [["employee", "=", employee["name"]], ["leave_type", "=", LEAVE_TYPE],
                       ["from_date", "=", FROM_DATE], ["to_date", "=", TO_DATE]]
            found = [row for row in self.erp.list("Leave Allocation", filters, ["name", "docstatus"])
                     if row.get("docstatus") != 2]  # a cancelled allocation does not count
            if found:
                # A pre-existing draft is not silently submitted or replaced.
                self.counts["unchanged"] += 1
                if found[0].get("docstatus") == 0:
                    self.problems.append((entry["position"], "matching Leave Allocation is draft"))
                continue
            allocation = {
                "naming_series": "HR-LAL-.YYYY.-", "employee": employee["name"], "company": COMPANY,
                "leave_type": LEAVE_TYPE, "from_date": FROM_DATE, "to_date": TO_DATE,
                "new_leaves_allocated": entry["days"], "carry_forward": 1,
            }
            if leave_period_name:
                allocation["leave_period"] = leave_period_name
            if not self.dry_run:
                try:
                    created = self.erp.insert("Leave Allocation", allocation)
                    self.erp.call("frappe.client.submit", doc=self.erp.get("Leave Allocation", created["name"]))
                except ErpError as err:
                    self.counts["failed"] += 1
                    self.problems.append((entry["position"], "Leave Allocation refused ({})".format(type(err).__name__)))
                    continue
            self.counts["created"] += 1


def write_defaults(path, defaults):
    """Private operator list of records whose entitlement was derived rather than exported."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("# default allocations, check with Benchi: {}\n".format(len(defaults)))
        for employee, days in defaults:
            stream.write("{}\t{}\t{}\n".format(employee.get("employee_name") or employee.get("name"),
                                                  employee.get("name"), days))


def main(argv):
    parser = argparse.ArgumentParser(description="Set up 2026 HRMS holidays and Ferien allocations.")
    parser.add_argument("--apply", action="store_true", help="write through the API (go-live bead only)")
    parser.add_argument("--dry-run", action="store_true", help="read and report only (default)")
    parser.add_argument("--token-file", default=TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    exports, _paystubs = load_payroll(os.path.join(PRIVATE, "bexio-payroll"))
    erp = Erp.from_file(args.token_file)
    dry_run = not args.apply
    importer = LeaveImporter(erp, dry_run=dry_run)
    try:
        all_employees = erp.list("Employee", fields=["name", "bexio_id", "status", "date_of_birth", "swiss_employment_level", "holiday_list", "employee_name"])
        company = erp.get("Company", COMPANY)
        by_id = {str(row.get("bexio_id")): row for row in all_employees if row.get("bexio_id")}
        plan, defaults, problems = build_plan(exports, by_id)
        importer.run_setup()
        importer.run_employees(plan)
        if company.get("default_holiday_list") != HOLIDAY_LIST:
            if dry_run:
                importer.counts["updated"] += 1
            else:
                erp.update("Company", COMPANY, {"default_holiday_list": HOLIDAY_LIST})
                importer.counts["updated"] += 1
        else:
            importer.counts["unchanged"] += 1
        write_defaults(DEFAULTS_FILE, defaults)
    except ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    for key, value in importer.counts.items():
        print("{}: {}".format(key, value))
    print("active employees: {}  defaults to check: {}".format(len(plan), len(defaults)))
    print("holiday dates: {} public, {} total with weekends".format(len(PUBLIC_HOLIDAYS), len(holiday_rows())))
    print("dry run: nothing was written" if dry_run else "written to ERPNext")
    print("private defaults list: {}".format(DEFAULTS_FILE))
    for position, problem in problems + importer.problems:
        print("record {}: {}".format(position, problem), file=sys.stderr)
    return 1 if importer.counts["failed"] or problems or importer.problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
