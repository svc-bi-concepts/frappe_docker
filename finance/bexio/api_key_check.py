"""Whether an ERPNext API key can read payroll: the proof of keys split 2 (erp-sbpk, erp-95bt).

    python3 finance/bexio/api_key_check.py <key file>

Read-only, standard library, through import_master.Erp. The agent key (.erpnext-api, beside the
key file under test; --lister-key names another) lists the doctypes of the Payroll, HR and BI
Payroll modules (the exact names in hrms's and bi_payroll's modules.txt; no single or child
doctype), and the check adds Employee, Version rows of Salary Slip and File rows attached to a
Salary Slip. The key under test then asks each for ONE row, field name only.

One line per doctype: refused (HTTP 403, Frappe's answer to a list without read), visible (a
row came back) or empty (allowed to list, no row there). Then a total. No row and no name is ever
printed, and neither is a key. Exit 0 when nothing is visible, 1 when anything is, 2 when a
call failed for another reason (the check then says nothing about that doctype).
"""

import argparse
import os
import sys

import import_master as im

# hrms: Payroll and HR hold the salary and employee doctypes; bi_payroll: BI Payroll.
MODULES = ["Payroll", "HR", "BI Payroll"]
# Doctypes outside those modules that carry payroll data, with the filter that narrows them.
EXTRA = [
    ("Employee", []),
    ("Version", [["ref_doctype", "=", "Salary Slip"]]),
    ("File", [["attached_to_doctype", "=", "Salary Slip"]]),
]


def targets(lister):
    """(doctype, filters) pairs to probe: the modules' own doctypes, then EXTRA."""
    names = lister.list(
        "DocType",
        [["module", "in", MODULES], ["issingle", "=", 0], ["istable", "=", 0]],
    )
    return [(row["name"], []) for row in sorted(names, key=lambda r: r["name"])] + EXTRA


def probe(erp, doctype, filters):
    """refused, visible or empty for one doctype; the row itself is dropped on the spot."""
    try:
        rows = erp.list(doctype, filters, fields=("name",), limit=1)
    except im.ErpError as err:
        if err.status == 403:
            return "refused"
        raise
    return "visible" if rows else "empty"


def check(lister, erp, out):
    results = [
        (doctype, probe(erp, doctype, filters)) for doctype, filters in targets(lister)
    ]
    for doctype, verdict in results:
        print(f"{verdict:8} {doctype}", file=out)
    counts = {
        v: sum(1 for _d, r in results if r == v)
        for v in ("visible", "refused", "empty")
    }
    print(
        "total {}: {} visible, {} refused, {} empty".format(
            len(results), counts["visible"], counts["refused"], counts["empty"]
        ),
        file=out,
    )
    return 1 if counts["visible"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "key_file",
        help="the key file under test, e.g. ~/ws_yardr_finance/.erpnext-api-mcp",
    )
    parser.add_argument(
        "--lister-key",
        help="the key that lists the doctypes (default: .erpnext-api beside the key file)",
    )
    args = parser.parse_args(argv)
    lister_key = args.lister_key or os.path.join(
        os.path.dirname(os.path.abspath(args.key_file)), ".erpnext-api"
    )
    try:
        return check(
            im.Erp.from_file(lister_key), im.Erp.from_file(args.key_file), sys.stdout
        )
    except im.ErpError as err:
        print(f"api_key_check: {err}; the result is incomplete", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
