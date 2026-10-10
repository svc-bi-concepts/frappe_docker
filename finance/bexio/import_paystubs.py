"""Attach the bexio paystub PDFs to their Employees, as private Files: the payslip history, as reference.

export.py --payroll writes one PDF per employee and month to <private>/bexio-payroll/paystubs/ and lists them
in paystubs.json (employee_id, year, month, file, bytes). Each PDF becomes one private ERPNext File on the
Employee with that bexio_id (import_employees.py), named <year>-<month>-paystub.pdf (no name of the person in
it), keyed paystub-<employee_id>-<year>-<month> in File.bexio_id. The key carries the employee's bexio id, so
a second run finds the File and skips it: the pass can be rerun.

No Salary Slip is made for these months: the export holds no figures per slip, and a slip without real amounts
would feed the payroll hand-over report and the Lohnausweis with wrong numbers (finance/docs/hrms.md, step 5).
A File posts no GL entry.

--dry-run (the default) reads ERPNext and writes nothing. --apply uploads each PDF whose Employee exists and
whose File is not there yet:

    python3 finance/bexio/import_paystubs.py [--dry-run] [--paystubs-json PATH]
    python3 finance/bexio/import_paystubs.py --apply [--paystubs-json PATH]

The dry run prints counts only. The records it cannot place (no Employee with their employee_id, a PDF that is
missing or of another size than the export's) are listed by their position in paystubs.json in
<private>/bexio-paystubs-dry-run.txt, never on the screen. A record whose Employee is missing is counted and not
uploaded; a PDF that is missing or of another size makes the run fail.

Standard library only, apart from import_files.py, import_master.py and import_purchase.py.
"""

import argparse
import json
import os
import sys

import import_files as imf
import import_master as im
import import_purchase as ip

PAYSTUBS_JSON = os.path.join(im.PRIVATE, "bexio-payroll", "paystubs.json")
PAYSTUBS_DIR = os.path.join(im.PRIVATE, "bexio-payroll", "paystubs")
PROBLEMS_FILE = "bexio-paystubs-dry-run.txt"
DOCTYPE = "Employee"
KEY_PREFIX = "paystub-"

ATTACH, ATTACHED, NO_EMPLOYEE = "attach", "attached", "no employee"
PROBLEMS = ("no content", "size differs")


def file_key(record):
    """The File.bexio_id of a paystub: its employee's bexio id, the year and the month. Starts with paystub-, so it
    collides with no invoice id, no kind-prefixed document key and no file uuid."""
    return "{}{}-{}-{:02d}".format(KEY_PREFIX, record["employee_id"], record["year"], record["month"])


def file_name(record):
    """The name the File is uploaded under: the year and the month only, no name of the person."""
    return "{}-{:02d}-paystub.pdf".format(record["year"], record["month"])


def load_records(path):
    """The rows of paystubs.json, or None when the export has none."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Lookups:
    """What the plan reads from ERPNext: the Employees by bexio_id, and the paystub keys already on a File."""

    def __init__(self, employees, keys):
        self.employees = employees  # bexio id -> Employee name
        self.keys = keys            # File.bexio_id values that start with KEY_PREFIX

    @classmethod
    def from_erp(cls, erp):
        employees = {r["bexio_id"]: r["name"] for r in erp.list("Employee", [["bexio_id", "is", "set"]], ["name", "bexio_id"])}
        keys = {f["bexio_id"] for f in erp.list("File", [["bexio_id", "is", "set"]], ["bexio_id"])
                if f["bexio_id"].startswith(KEY_PREFIX)}
        return cls(employees, keys)


def content_path(paystubs_dir, record):
    return os.path.join(paystubs_dir, record["file"])


def plan(records, lookups, paystubs_dir):
    """One row per record of paystubs.json: its outcome, the Employee it goes to, and its File key and name."""
    rows = []
    for position, record in enumerate(records, 1):
        key = file_key(record)
        employee = lookups.employees.get(str(record["employee_id"]))
        outcome = None
        if key in lookups.keys:
            outcome = ATTACHED
        elif employee is None:
            outcome = NO_EMPLOYEE
        else:
            path = content_path(paystubs_dir, record)
            if not os.path.isfile(path):
                outcome = "no content"
            elif os.path.getsize(path) != record["bytes"]:
                outcome = "size differs"
            else:
                outcome = ATTACH
        rows.append({"position": position, "key": key, "name": file_name(record), "file": record["file"],
                     "employee_id": str(record["employee_id"]), "employee": employee,
                     "year": record["year"], "month": record["month"], "bytes": record["bytes"], "outcome": outcome})
    return rows


def run(records, erp, paystubs_dir):
    """Plan every record against ERPNext: the rows, and the Lookups they were planned with."""
    lookups = Lookups.from_erp(erp)
    return plan(records, lookups, paystubs_dir), lookups


def apply(rows, paystubs_dir, erp):
    """Upload each PDF the plan marks to attach, as a private File on its Employee. Returns how many were uploaded."""
    uploaded = 0
    for row in rows:
        if row["outcome"] != ATTACH:
            continue
        with open(os.path.join(paystubs_dir, row["file"]), "rb") as f:
            imf.upload(erp, row["employee"], row["key"], row["name"], f.read(), doctype=DOCTYPE)
        uploaded += 1
    return uploaded


def employees_of(rows):
    """The distinct employee ids of the export, and how many of them have an Employee in ERPNext."""
    ids = {r["employee_id"] for r in rows}
    found = {r["employee_id"] for r in rows if r["employee"] is not None}
    return ids, found


def report(rows, applied=False):
    """Counts only: no ids, no names, no file names."""
    ids, found = employees_of(rows)
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    lines = ["paystubs: {} record(s)".format(len(rows)),
             "employees: {} in the export, {} found in ERPNext, {} missing".format(len(ids), len(found), len(ids) - len(found))]
    for outcome in (ATTACH, ATTACHED, NO_EMPLOYEE) + PROBLEMS:
        if counts.get(outcome):
            lines.append("  {:<14}{:>6}".format(outcome, counts[outcome]))
    lines.append("applied: the PDFs marked attach are uploaded as private Files on their Employees" if applied
                 else "dry run: nothing was written")
    return "\n".join(lines)


def problem_line(row):
    return "paystub record {}: {}".format(row["position"], row["outcome"])


def main(argv):
    parser = argparse.ArgumentParser(description="Attach the bexio paystub PDFs to their Employees (a dry run by default).")
    parser.add_argument("--paystubs-json", default=PAYSTUBS_JSON, help="the export's paystubs.json")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the counts (the default)")
    parser.add_argument("--apply", action="store_true", help="upload the PDFs that would be attached, as private Files")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are one or the other")
    paystubs_dir = os.path.join(os.path.dirname(args.paystubs_json), "paystubs")
    records = load_records(args.paystubs_json)
    if records is None:
        print("aborted: no {} (run export.py --payroll first)".format(args.paystubs_json), file=sys.stderr)
        return 2
    try:
        erp = im.Erp.from_file(args.token_file)
        rows, _ = run(records, erp, paystubs_dir)
        if args.apply:
            print("uploaded {} paystub PDF(s)".format(apply(rows, paystubs_dir, erp)))
            rows, _ = run(records, erp, paystubs_dir)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print(report(rows, applied=args.apply))
    listed = [r for r in rows if r["outcome"] in (NO_EMPLOYEE,) + PROBLEMS]
    if listed:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), [problem_line(r) for r in listed])
        print("{} line(s) listed in {}".format(len(listed), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    return 1 if any(r["outcome"] in PROBLEMS for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
