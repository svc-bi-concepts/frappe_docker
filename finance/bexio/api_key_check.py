"""Whether an ERPNext API key can read payroll: the proof of keys split 2 (erp-sbpk, erp-95bt).

    python3 finance/bexio/api_key_check.py <key file>

Read-only, standard library, through import_master.Erp. The agent key (im.TOKEN_FILE, so
.erpnext-api) lists the doctypes of every module in hrms's modules.txt and of BI Payroll (no
single or child doctype), and the check adds Employee, Deleted Document and Version with no filter
(a key that reads them reads whole documents, and today's rows prove nothing about tomorrow's) and
File on Salary Slip and on Employee, where the paystub PDFs are. The key under test then asks each
for ONE row, field name only.

Then the payroll and HR reports that were open to the key roles (keyaccess in swiss-setup.py closes them
with a Custom Role): each is asked through frappe.desk.query_report.get_script, which passes the
same gate as the report (Report.is_permitted, then report permission on its doctype) and returns no
report data. The report itself is never run, and the script that comes back is dropped on the spot.

Then Payment Proposal's two child doctypes, listed directly with parent=Payment Proposal and asked for employee and
amount (keyaccess raises those fields to permlevel 1): refused, empty (no row, or a row with its name alone, which is
what Frappe returns once the fields are dropped) or visible (a row carrying employee or amount).

One "level1" line: visible when a key role holds a permlevel 1 row on Payment Proposal (read through the agent
key), which would reopen the child fields; refused when none does.

Then two whitelisted methods the method gate (bi_finance/api_gate.py, erp-kb06) must refuse: erpnext's
employee_query (employee names, no role check) and frappe.desk.search.search_link with query set to it (it
calls any whitelisted query by name), both with a page length of 1. Only reads are asked: a function that
writes is never probed, without the gate it would insert a Payment Proposal. An answer is dropped on the spot.

One line per doctype: refused (HTTP 403, Frappe's answer to a list without read), visible (a
row came back) or empty (allowed to list, no row there); the File lines say which filter ran. One
line per report, starting "report", and one per method, starting "method": refused (403) or visible (an
answer came back). One line per child doctype, starting "child". Then a total.
No row and no name is ever printed, and neither is a key.

Exit 0 only when every line is refused (reports and methods included), the File and child lines alone may also be
empty:
File is filtered row by row, so anyone may list it. Exit 1 otherwise: empty on another line means
the key may list that doctype, and the rows come later. Exit 2 when the check could not finish (a
call failed for another reason, a key file is unreadable, or a module that holds doctypes listed
none, which would otherwise pass by probing nothing).
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
# Payment Proposal's two child doctypes, listed directly with parent=Payment Proposal (erp-btgr): the key may not
# read employee or amount from them. Empty passes, as the File lines do: with no proposal there is no row to show.
CHILDREN = [
    ("Payment Proposal Salary Slip", "Payment Proposal", ("employee", "amount")),
    ("Payment Proposal Expense", "Payment Proposal", ("employee", "amount")),
]
# The roles of the sync key (the longer list in create-api-user.sh; test_swiss_setup compares it with keyaccess's
# KEY_ROLES). A direct child list is filtered by the parent's permissions, so the child fields stay closed only
# while none of them holds a level 1 row on Payment Proposal (erp-btgr).
KEY_ROLES = [
    "Accounts User",
    "Sales User",
    "Purchase User",
    "Stock User",
    "Sales Master Manager",
    "Purchase Master Manager",
    "Item Manager",
]
# Doctypes outside those modules that carry payroll data, with the filter that narrows them.
EXTRA = [
    ("Employee", []),
    ("Deleted Document", []),
    ("Version", []),
    ("File", [["attached_to_doctype", "=", "Salary Slip"]]),
    ("File", [["attached_to_doctype", "=", "Employee"]]),
]
# Reports that read payroll or HR and are asked besides the doctypes: Cash Flow Forecast (net pay per run, raw SQL),
# Cash Flow Forecast Lines (the same computation, line by line), Project Profitability (gross pay per slip), and
# Monthly Worktime and Worktime Overview (given an employee id, they read it past the Employee permission).
# swiss-setup.py's KEY_REPORTS closes the same ones.
REPORTS = [
    "Cash Flow Forecast",
    "Cash Flow Forecast Lines",
    "Project Profitability",
    "Monthly Worktime",
    "Worktime Overview",
]
EMPLOYEE_QUERY = "erpnext.controllers.queries.employee_query"
# Methods the gate refuses, read-only ones only: (method, arguments, what the line adds to the name). The second
# one reaches the first through search_widget, which no override of a whitelisted method covers.
METHODS = [
    (
        EMPLOYEE_QUERY,
        {
            "doctype": "Employee",
            "txt": "",
            "searchfield": "name",
            "start": 0,
            "page_len": 1,
            "filters": None,
        },
        "",
    ),
    (
        "frappe.desk.search.search_link",
        {"doctype": "Employee", "txt": "", "query": EMPLOYEE_QUERY, "page_length": 1},
        " (query employee_query)",
    ),
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


def probe_method(erp, method, **args):
    """refused or visible for one whitelisted method; its answer is dropped on the spot."""
    try:
        erp.call(method, **args)
    except im.ErpError as err:
        if err.status == 403:
            return "refused"
        raise
    return "visible"


def probe_report(erp, report):
    """refused or visible for one report; get_script never runs it."""
    return probe_method(erp, "frappe.desk.query_report.get_script", report_name=report)


def probe_child(erp, doctype, parent, fields):
    """refused or visible or empty for a child doctype listed directly with its parent doctype named: visible
    only when a row came back carrying one of the person or money fields. Frappe drops a field the key may not
    read, so a row with the name alone counts as empty. The row is dropped on the spot.
    """
    try:
        rows = erp.list(doctype, [], fields=fields, limit=1, parent=parent)
    except im.ErpError as err:
        if err.status == 403:
            return "refused"
        raise
    return "visible" if any(row.get(f) for row in rows for f in fields) else "empty"


def probe_level1(lister, doctype, key_roles):
    """visible when a key role holds a permlevel 1 row on the doctype, else refused. Roles are not printed."""
    rows = lister.meta(doctype).get("permissions", [])
    held = any(r.get("permlevel") == 1 and r.get("role") in key_roles for r in rows)
    return "visible" if held else "refused"


def label(doctype, filters):
    """The doctype, and the filter when one ran, so the two File lines can be told apart."""
    return " ".join([doctype] + [f"({f} {op} {v})" for f, op, v in filters])


def check(lister, erp, out):
    # (line, verdict, whether empty passes): only the File lines may be empty
    results = []
    for doctype, filters in targets(lister):
        verdict = probe(erp, doctype, filters)
        line = f"{verdict:8} {label(doctype, filters)}"
        results.append((line, verdict, doctype == "File"))
    for name in REPORTS:
        verdict = probe_report(erp, name)
        results.append((f"report   {verdict:8} {name}", verdict, False))
    for doctype, parent, fields in CHILDREN:
        verdict = probe_child(erp, doctype, parent, fields)
        results.append((f"child    {verdict:8} {doctype}", verdict, True))
    verdict = probe_level1(lister, "Payment Proposal", KEY_ROLES)
    results.append(
        (f"level1   {verdict:8} Payment Proposal, key roles", verdict, False)
    )
    for method, args, note in METHODS:
        verdict = probe_method(erp, method, **args)
        results.append((f"method   {verdict:8} {method}{note}", verdict, False))
    for line, _verdict, _ok_empty in results:
        print(line, file=out)
    counts = {
        v: sum(1 for _l, r, _e in results if r == v)
        for v in ("visible", "refused", "empty")
    }
    print(
        "total {}: {} visible, {} refused, {} empty".format(
            len(results), counts["visible"], counts["refused"], counts["empty"]
        ),
        file=out,
    )
    passed = all(r == "refused" or (r == "empty" and ok) for _l, r, ok in results)
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
