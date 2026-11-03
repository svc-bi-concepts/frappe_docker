"""Whether an ERPNext API key can read payroll: the proof of keys split 2 (erp-sbpk, erp-95bt).

    python3 finance/bexio/api_key_check.py <key file>

Read-only, standard library, through import_master.Erp. The agent key (im.TOKEN_FILE, so
.erpnext-api) lists the doctypes of every module in hrms's modules.txt and of BI Payroll (no
single or child doctype), and the check adds Employee, Deleted Document and Version with no filter
(a key that reads them reads whole documents, and today's rows prove nothing about tomorrow's) and
File on Salary Slip and on Employee, where the paystub PDFs are. The key under test then asks each
for ONE row, field name only.

One line per doctype: refused (HTTP 403, Frappe's answer to a list without read), visible (a
row came back) or empty (allowed to list, no row there); the File lines say which filter ran. Then
a total. No row and no name is ever printed, and neither is a key.

Exit 0 only when every line is refused, the File lines alone may also be empty: File is filtered
row by row, so anyone may list it. Exit 1 otherwise: empty on another line means the key may list
that doctype, and the rows come later. Exit 2 when the check could not finish (a call failed for
another reason, a key file is unreadable, or a module that holds doctypes listed none, which would
otherwise pass by probing nothing).
"""

import argparse
import sys

import import_master as im

# hrms's modules.txt, then bi_payroll's. No payroll, HR or Employee reads through a key (mem-g98v), so all of them.
MODULES = [
    "HR Setup",
    "Tenure",
    "Recruitment",
    "Shift and Attendance",
    "Leaves",
    "Expenses",
    "Performance",
    "Payroll",
    "Tax and Benefits",
    "HR",
    "BI Payroll",
]
# The modules that hold doctypes today; the other hrms entries list none yet, and that is no failure.
MUST_LIST = ["Payroll", "HR", "BI Payroll"]
# Doctypes outside those modules that carry payroll data, with the filter that narrows them.
EXTRA = [
    ("Employee", []),
    ("Deleted Document", []),
    ("Version", []),
    ("File", [["attached_to_doctype", "=", "Salary Slip"]]),
    ("File", [["attached_to_doctype", "=", "Employee"]]),
]


def targets(lister):
    """(doctype, filters) pairs to probe: the modules' own doctypes, then EXTRA."""
    names = lister.list(
        "DocType",
        [["module", "in", MODULES], ["issingle", "=", 0], ["istable", "=", 0]],
        fields=("name", "module"),
    )
    missing = set(MUST_LIST) - {row["module"] for row in names}
    if missing:
        raise ValueError("no doctype listed for {}".format(", ".join(sorted(missing))))
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


def label(doctype, filters):
    """The doctype, and the filter when one ran, so the two File lines can be told apart."""
    return " ".join([doctype] + [f"({f} {op} {v})" for f, op, v in filters])


def check(lister, erp, out):
    results = [
        (doctype, filters, probe(erp, doctype, filters))
        for doctype, filters in targets(lister)
    ]
    for doctype, filters, verdict in results:
        print(f"{verdict:8} {label(doctype, filters)}", file=out)
    counts = {
        v: sum(1 for _d, _f, r in results if r == v)
        for v in ("visible", "refused", "empty")
    }
    print(
        "total {}: {} visible, {} refused, {} empty".format(
            len(results), counts["visible"], counts["refused"], counts["empty"]
        ),
        file=out,
    )
    passed = all(
        r == "refused" or (r == "empty" and doctype == "File")
        for doctype, _f, r in results
    )
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "key_file",
        help="the key file under test, e.g. ~/ws_yardr_finance/.erpnext-api-mcp",
    )
    args = parser.parse_args(argv)
    try:
        return check(
            im.Erp.from_file(im.TOKEN_FILE),
            im.Erp.from_file(args.key_file),
            sys.stdout,
        )
    except (im.ErpError, OSError, KeyError, ValueError) as err:
        print(f"api_key_check: {err!r}; the result is incomplete", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
