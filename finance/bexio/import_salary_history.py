"""Load submitted Salary Slip snapshots from private parsed bexio paystub figures.

Dry run by default. The figures and per-position problems stay under private/payroll;
stdout contains counts only. Requires the Salary Slip history methods in bi_payroll.
"""

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

import import_master as im
import import_purchase as ip

FIGURES = os.path.join(im.PRIVATE, "payroll", "paystub-figures.csv")
PROBLEMS_FILE = os.path.join(im.PRIVATE, "payroll", "salary-history-problems.txt")
KEY_PREFIX = "paystub-"
ZERO = Decimal("0")

# CSV wage type -> (Salary Component, earning, included in slip totals)
WAGE_TYPES = {
    "1000": ("Basic Salary", True, True),
    "1001": ("Basic Salary", True, True),
    "1005": ("Hourly Wage", True, True),
    "1160": ("Vacation Compensation", True, True),
    "1218": ("Commission", True, True),
    "5010": ("AHV/IV/EO Employee", False, True),
    "5020": ("ALV Employee", False, True),
    "5040": ("NBU Employee", False, True),
    "5050": ("BVG Employee", False, True),
    "5060": ("Quellensteuer Employee", False, True),
    "6030": ("Expenses Effective", True, False),
    "6070": ("Expenses Lump Sum", True, False),
    "6300.1": ("Rent Deduction", False, False),
    "6510": ("Advance Payment", False, False),
}
TOTAL_TYPES = {"5000", "5900", "6500", "6600"}
LOAD, LOADED, DIFFERS = "load", "loaded", "differs"
BAD = ("unknown wage type", "sums differ", "no employee", "after cutover")


def file_key(employee_id, year, month):
    return f"{KEY_PREFIX}{employee_id}-{int(year)}-{int(month):02d}"


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid figure") from None
    if not result.is_finite():
        raise ValueError("invalid figure")
    return result


def read_figures(path):
    with open(path, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


class Lookups:
    def __init__(self, employees, slips):
        self.employees, self.slips = employees, slips

    @classmethod
    def from_erp(cls, erp):
        employees = {
            str(r["bexio_id"]): r["name"]
            for r in erp.list(
                "Employee", [["bexio_id", "is", "set"]], ["name", "bexio_id"]
            )
        }
        slips = {
            r["swiss_bexio_id"]: r
            for r in erp.list(
                "Salary Slip",
                [["swiss_bexio_id", "is", "set"]],
                [
                    "name",
                    "swiss_bexio_id",
                    "gross_pay",
                    "total_deduction",
                    "net_pay",
                    "rounded_total",
                    "is_proposed",
                ],
            )
        }
        # Child count is compared separately; a single bounded list avoids fetching names or row data.
        for slip in slips.values():
            slip["row_count"] = len(
                erp.list("Salary Detail", [["parent", "=", slip["name"]]], ["name"])
            )
        return cls(employees, slips)


def group_figures(records):
    grouped = {}
    for position, source in enumerate(records, 1):
        try:
            employee_id, year, month = (
                str(source["employee_id"]),
                int(source["year"]),
                int(source["month"]),
            )
            wage_type = str(source["wage_type"])
            amount = decimal(source["amount"])
            date(year, month, 1)
        except (KeyError, ValueError, TypeError):
            raise ValueError("invalid CSV data") from None
        key = (employee_id, year, month)
        stub = grouped.setdefault(
            key, {"position": position, "rows": [], "totals": {}, "unknown": False}
        )
        stub.setdefault("source_rows", []).append(source)
        if wage_type in TOTAL_TYPES:
            if wage_type in stub["totals"]:
                stub["malformed"] = True
            stub["totals"][wage_type] = amount
        elif wage_type not in WAGE_TYPES:
            stub["unknown"] = True
        else:
            component, earning, included = WAGE_TYPES[wage_type]
            # Printed deductions are negative; Salary Slip deductions are positive amounts.
            signed_amount = amount if earning else -amount
            stub["rows"].append(
                {
                    "salary_component": component,
                    "amount": str(signed_amount),
                    "do_not_include_in_total": int(not included),
                    "earning": earning,
                    "included": included,
                }
            )
    return grouped


def sums_hold(stub):
    totals = stub["totals"]
    if set(totals) != TOTAL_TYPES:
        return False
    earnings = sum(
        (decimal(r["amount"]) for r in stub["rows"] if r["earning"] and r["included"]),
        ZERO,
    )
    deductions = sum(
        (
            decimal(r["amount"])
            for r in stub["rows"]
            if not r["earning"] and r["included"]
        ),
        ZERO,
    )
    after_net = sum(
        (
            decimal(r["amount"]) if r["earning"] else -decimal(r["amount"])
            for r in stub["rows"]
            if not r["included"]
        ),
        ZERO,
    )
    return (
        earnings == totals["5000"]
        and deductions == -totals["5900"]
        and earnings - deductions == totals["6500"]
        and totals["6500"] + after_net == totals["6600"]
    )


def payload(key, employee, year, month, stub):
    last = date(year + (month == 12), month % 12 + 1, 1).toordinal() - 1
    end = date.fromordinal(last).isoformat()
    start = date(year, month, 1).isoformat()
    earnings, deductions = [], []
    for row in stub["rows"]:
        item = {
            k: row[k] for k in ("salary_component", "amount", "do_not_include_in_total")
        }
        (earnings if row["earning"] else deductions).append(item)
    t = stub["totals"]
    return {
        "bexio_id": key,
        "employee": employee,
        "start_date": start,
        "end_date": end,
        "gross_pay": str(t["5000"]),
        "total_deduction": str(-t["5900"]),
        "net_pay": str(t["6500"]),
        "paid_out": str(t["6600"]),
        "earnings": earnings,
        "deductions": deductions,
    }


def plan(records, lookups):
    grouped = group_figures(records)
    rows = []
    for (employee_id, year, month), stub in sorted(
        grouped.items(), key=lambda item: item[0][1:] + (item[0][0],)
    ):
        key = file_key(employee_id, year, month)
        outcome = "unknown wage type" if stub["unknown"] else None
        if outcome is None and stub.get("malformed"):
            outcome = "sums differ"
        if outcome is None and not sums_hold(stub):
            outcome = "sums differ"
        if outcome is None and (year, month) > (2026, 12):
            outcome = "after cutover"
        employee = lookups.employees.get(employee_id)
        if outcome is None and employee is None:
            outcome = "no employee"
        slip = lookups.slips.get(key)
        item = {
            "position": stub["position"],
            "key": key,
            "stub": stub,
            "employee": employee,
            "year": year,
            "month": month,
            "outcome": outcome,
        }
        if outcome is None:
            expected = payload(key, employee, year, month, stub)
            if slip:
                fields = (
                    ("gross_pay", "gross_pay"),
                    ("total_deduction", "total_deduction"),
                    ("net_pay", "net_pay"),
                    ("paid_out", "rounded_total"),
                )
                equal = all(
                    decimal(slip.get(actual, 0)) == decimal(expected[source])
                    for source, actual in fields
                )
                equal = equal and int(slip.get("row_count", -1)) == len(stub["rows"])
                item["outcome"] = LOADED if equal else DIFFERS
            else:
                item["outcome"] = LOAD
            item["payload"] = expected
        rows.append(item)
    return rows


def apply(rows, erp):
    for row in rows:
        if row["outcome"] == LOAD:
            erp.call("bi_payroll.salary_history.load_slip", slip=row["payload"])


def report(rows, action="dry run"):
    counts = Counter(r["outcome"] for r in rows)
    return "salary history: {} stub(s)\n{}\n{}".format(
        len(rows),
        "\n".join(
            f"  {name:<18}{counts[name]:>6}"
            for name in (LOAD, LOADED, DIFFERS) + BAD
            if counts[name]
        ),
        action,
    )


def check_rows(rows, lookups):
    """Check proposal protection on every keyed slip, including keys outside this CSV."""
    proposal_errors = sum(
        1 for slip in lookups.slips.values() if int(slip.get("is_proposed") or 0) != 1
    )
    for row in rows:
        if (
            row["outcome"] == LOADED
            and int(lookups.slips[row["key"]].get("is_proposed") or 0) != 1
        ):
            row["outcome"] = DIFFERS
    return proposal_errors


def write_problems(path, rows):
    lines = []
    for row in rows:
        if row["outcome"] not in BAD + (DIFFERS,):
            continue
        line = f"stub position {row['position']}: {row['outcome']}"
        if row["outcome"] == "unknown wage type":
            unknown = sorted(
                {
                    str(source["wage_type"])
                    for source in row["stub"].get("source_rows", [])
                    if str(source["wage_type"]) not in WAGE_TYPES
                    and str(source["wage_type"]) not in TOTAL_TYPES
                }
            )
            if unknown:
                line += " (" + ", ".join(unknown) + ")"
        lines.append(line)
    ip.write_private(path, lines)


def remove_month(erp, year, month, apply_changes):
    slips = [
        slip
        for slip in erp.list(
            "Salary Slip", [["swiss_bexio_id", "is", "set"]], ["name", "swiss_bexio_id"]
        )
        if slip["swiss_bexio_id"].startswith(KEY_PREFIX)
        and slip["swiss_bexio_id"].endswith(f"-{year}-{month:02d}")
    ]
    if apply_changes:
        for slip in slips:
            erp.call(
                "bi_payroll.salary_history.remove_slip", bexio_id=slip["swiss_bexio_id"]
            )
    print(
        f"remove-month {year}-{month:02d}: {len(slips)} slip(s) {'removed' if apply_changes else 'would be removed'}"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Load historical bexio paystub Salary Slips (dry run by default)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--remove-month", metavar="YYYY-MM")
    parser.add_argument("--figures", default=FIGURES)
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.remove_month:
        try:
            year, month = map(int, args.remove_month.split("-"))
            date(year, month, 1)
        except (ValueError, TypeError):
            parser.error("--remove-month must be YYYY-MM")
        erp = im.Erp.from_file(args.token_file)
        return remove_month(erp, year, month, args.apply)
    records = read_figures(args.figures)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = Lookups.from_erp(erp)
        rows = plan(records, lookups)
        if args.apply and any(r["outcome"] in BAD + (DIFFERS,) for r in rows):
            print(report(rows, "apply refused: no slips written"))
            write_problems(PROBLEMS_FILE, rows)
            return 1
        if args.check:
            proposal_errors = check_rows(rows, lookups)
            ok = all(r["outcome"] == LOADED for r in rows) and proposal_errors == 0
            print(report(rows, "check passed" if ok else "check failed"))
            if proposal_errors:
                print(f"  is_proposed not set: {proposal_errors}")
            write_problems(PROBLEMS_FILE, rows)
            return 0 if ok else 1
        if args.apply:
            apply(rows, erp)
            rows = plan(records, Lookups.from_erp(erp))
        print(report(rows, "applied" if args.apply else "dry run: nothing was written"))
        write_problems(PROBLEMS_FILE, rows)
        return 1 if any(r["outcome"] in BAD + (DIFFERS,) for r in rows) else 0
    except (im.ErpError, ValueError) as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
