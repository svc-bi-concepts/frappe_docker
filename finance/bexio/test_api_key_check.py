"""Offline tests for api_key_check.py: a fake ERPNext that refuses as Frappe does. Invented doctypes and rows only.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import email.message
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import api_key_check as akc
import import_master as im
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)

PAYROLL_DOCTYPES = ["Payroll Entry", "Salary Slip", "Salary Structure"]
HR_DOCTYPES = ["Appraisal Cycle", "Leave Type"]
BI_PAYROLL_DOCTYPES = ["Qst Code"]
EMPLOYEE_QUERY = "erpnext.controllers.queries.employee_query"
SEARCH_LINK = "frappe.desk.search.search_link"
GET_SCRIPT = "frappe.desk.query_report.get_script"
BI_FINANCE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "apps", "bi_finance"
)
with mock.patch.dict(
    sys.modules, {"frappe": types.ModuleType("frappe")}
), mock.patch.object(sys, "path", [BI_FINANCE] + sys.path):
    from bi_finance import api_gate
ON_SALARY_SLIP = "File (attached_to_doctype = Salary Slip)"
ON_EMPLOYEE = "File (attached_to_doctype = Employee)"
REPORTS = [
    "Cash Flow Forecast",
    "Cash Flow Forecast Lines",
    "Project Profitability",
    "Monthly Worktime",
    "Worktime Overview",
]
CHILDREN = ["Payment Proposal Salary Slip", "Payment Proposal Expense"]
ALL_PROBED = sorted(PAYROLL_DOCTYPES + HR_DOCTYPES + BI_PAYROLL_DOCTYPES) + [
    "Employee",
    "Deleted Document",
    "Version",
    ON_SALARY_SLIP,
    ON_EMPLOYEE,
]
# Every module listed, from hrms's modules.txt and bi_payroll's.
ALL_MODULES = {
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
}


class FakeSite:
    """list() as Frappe answers it: 403 for a doctype the user cannot read, else the rows (one per call at most).
    call() likewise: get_script answers 403 for a report the user's roles do not open, else a script; the two gated
    methods answer 403 as the method gate does, else a row of invented names; any other method is a failure.
    """

    def __init__(
        self,
        readable=(),
        rows=None,
        modules=None,
        open_reports=(),
        child_rows=None,
        open_methods=(),
    ):
        self.readable = set(readable)
        self.open_reports = set(open_reports)
        self.open_methods = set(open_methods)
        self.methods = []
        self.method_args = {}
        self.rows = rows or {}
        self.permissions = [
            {"role": "Accounts User", "permlevel": 0},
            {"role": "Accounts Manager", "permlevel": 1},
        ]
        self.calls = []
        self.child_calls = []
        # a child doctype listed with its parent: the fields a row comes back with (Frappe drops the others)
        self.child_rows = child_rows or {}
        self.modules = modules or {
            "Payroll": PAYROLL_DOCTYPES,
            "HR": HR_DOCTYPES,
            "BI Payroll": BI_PAYROLL_DOCTYPES,
        }

    def list(self, doctype, filters=None, fields=("name",), limit=100000, parent=None):
        if parent:
            self.child_calls.append((doctype, parent, tuple(fields), limit))
            if doctype not in self.readable:
                raise im.ErpError(
                    403, f"GET /resource/{doctype}: frappe.exceptions.PermissionError"
                )
            return [dict(r) for r in self.child_rows.get(doctype, [])][:limit]
        self.calls.append((doctype, filters, tuple(fields), limit))
        if doctype == "DocType":
            wanted = {m for _f, _op, ms in filters if _f == "module" for m in ms}
            assert wanted == ALL_MODULES, wanted
            return [
                {"name": name, "module": module}
                for module, names in self.modules.items()
                for name in names
            ]
        if doctype not in self.readable:
            raise im.ErpError(
                403, f"GET /resource/{doctype}: frappe.exceptions.PermissionError"
            )
        return (
            [{"name": "SECRET-ROW-NAME"}][:limit]
            if self.rows.get(doctype, True)
            else []
        )

    def meta(self, doctype):
        assert doctype == "Payment Proposal"
        return {"name": doctype, "permissions": self.permissions}

    def call(self, method, **args):
        self.methods.append(method)
        self.method_args[method] = args
        if method in (EMPLOYEE_QUERY, SEARCH_LINK):
            if method not in self.open_methods:
                raise im.ErpError(
                    403, f"POST /method/{method}: frappe.exceptions.PermissionError"
                )
            return [["SECRET-EMPLOYEE-NAME", "SECRET-EMPLOYEE-TITLE"]]
        if method != GET_SCRIPT:
            raise AssertionError(f"unexpected method {method}")
        if args["report_name"] not in self.open_reports:
            raise im.ErpError(
                403, f"POST /method/{method}: frappe.exceptions.PermissionError"
            )
        return {"script": "SECRET-SCRIPT", "filters": "SECRET-FILTERS"}


class ApiKeyCheck(unittest.TestCase):
    def run_check(self, probed, lister=None):
        out = io.StringIO()
        code = akc.check(lister or FakeSite(), probed, out)
        return code, out.getvalue()

    def test_every_line_refused_exits_0(self):
        code, text = self.run_check(FakeSite())
        self.assertEqual(code, 0)
        self.assertIn("refused  Salary Slip", text)
        self.assertIn("total 21: 0 visible, 21 refused, 0 empty", text)

    def test_empty_file_lines_exit_0(self):
        probed = FakeSite(readable={"File"}, rows={"File": False})
        code, text = self.run_check(probed)
        self.assertEqual(code, 0)
        self.assertIn(f"empty    {ON_SALARY_SLIP}", text)
        self.assertIn(f"empty    {ON_EMPLOYEE}", text)
        self.assertIn("total 21: 0 visible, 19 refused, 2 empty", text)

    def test_a_visible_file_line_exits_1(self):
        code, text = self.run_check(FakeSite(readable={"File"}))
        self.assertEqual(code, 1)
        self.assertIn(f"visible  {ON_EMPLOYEE}", text)

    def test_a_module_doctype_the_key_may_list_with_no_rows_exits_1(self):
        probed = FakeSite(readable={"Leave Type"}, rows={"Leave Type": False})
        code, text = self.run_check(probed)
        self.assertEqual(code, 1)
        self.assertIn("empty    Leave Type", text)
        self.assertIn("total 21: 0 visible, 20 refused, 1 empty", text)

    def test_empty_on_the_unfiltered_extras_exits_1(self):
        for doctype in ("Employee", "Deleted Document", "Version"):
            with self.subTest(doctype=doctype):
                code, _text = self.run_check(
                    FakeSite(readable={doctype}, rows={doctype: False})
                )
                self.assertEqual(code, 1)

    def test_a_readable_deleted_document_is_visible_and_exits_1(self):
        code, text = self.run_check(FakeSite(readable={"Deleted Document"}))
        self.assertEqual(code, 1)
        self.assertIn("visible  Deleted Document", text)

    def test_one_visible_doctype_exits_1_and_is_named(self):
        code, text = self.run_check(FakeSite(readable={"Salary Slip"}))
        self.assertEqual(code, 1)
        self.assertIn("visible  Salary Slip", text)
        self.assertIn("1 visible, 20 refused", text)

    def test_every_doctype_gets_a_line_in_order(self):
        _code, text = self.run_check(FakeSite())
        self.assertEqual(
            [
                line.split(" ", 1)[1].strip()
                for line in text.splitlines()[: len(ALL_PROBED)]
            ],
            ALL_PROBED,
        )

    def test_the_key_under_test_asks_for_one_name_per_doctype_with_the_narrowing_filters(
        self,
    ):
        probed = FakeSite()
        self.run_check(probed)
        self.assertEqual({fields for _d, _f, fields, _l in probed.calls}, {("name",)})
        self.assertEqual({limit for _d, _f, _fields, limit in probed.calls}, {1})
        filters = [(d, f) for d, f, _fields, _l in probed.calls]
        self.assertIn(("Version", []), filters)
        self.assertIn(("Deleted Document", []), filters)
        self.assertIn(("File", [["attached_to_doctype", "=", "Salary Slip"]]), filters)
        self.assertIn(("File", [["attached_to_doctype", "=", "Employee"]]), filters)

    def test_the_doctype_list_leaves_out_single_and_child_doctypes(self):
        lister = FakeSite()
        akc.targets(lister)
        filters = lister.calls[0][1]
        self.assertIn(["issingle", "=", 0], filters)
        self.assertIn(["istable", "=", 0], filters)

    def test_no_row_and_no_name_is_printed(self):
        readable = set(ALL_PROBED) | {"File"}
        _code, text = self.run_check(FakeSite(readable=readable, open_reports=REPORTS))
        self.assertNotIn("SECRET-ROW-NAME", text)
        self.assertNotIn("SECRET-SCRIPT", text)
        self.assertNotIn("SECRET-FILTERS", text)
        self.assertNotIn("SECRET-EMPLOYEE", text)

    def test_refused_reports_get_a_report_line_each_and_count(self):
        code, text = self.run_check(FakeSite())
        self.assertEqual(code, 0)
        for name in REPORTS:
            self.assertIn(f"report   refused  {name}", text.splitlines())

    def test_an_open_report_is_visible_and_exits_1(self):
        for name in REPORTS:
            with self.subTest(report=name):
                code, text = self.run_check(FakeSite(open_reports={name}))
                self.assertEqual(code, 1)
                self.assertIn(f"report   visible  {name}", text.splitlines())
                self.assertIn("1 visible, 20 refused", text)

    def test_the_child_doctypes_are_listed_with_their_parent_and_both_fields(self):
        probed = FakeSite()
        self.run_check(probed)
        self.assertEqual(
            probed.child_calls,
            [
                (name, "Payment Proposal", ("employee", "amount"), 1)
                for name in CHILDREN
            ],
        )

    def test_refused_child_doctypes_get_a_line_each_and_count(self):
        code, text = self.run_check(FakeSite())
        self.assertEqual(code, 0)
        for name in CHILDREN:
            self.assertIn(f"child    refused  {name}", text.splitlines())

    def test_a_child_row_with_employee_or_amount_is_visible_and_exits_1(self):
        for field in ("employee", "amount"):
            with self.subTest(field=field):
                site = FakeSite(
                    readable=CHILDREN,
                    child_rows={CHILDREN[0]: [{"name": "x", field: "SECRET-VALUE"}]},
                )
                code, text = self.run_check(site)
                self.assertEqual(code, 1)
                self.assertIn(f"child    visible  {CHILDREN[0]}", text.splitlines())
                self.assertNotIn("SECRET-VALUE", text)

    def test_a_child_row_with_only_its_name_is_empty_and_passes(self):
        # Frappe drops the fields the key may not read: the name alone is what comes back.
        site = FakeSite(
            readable=CHILDREN,
            child_rows={n: [{"name": "SECRET-ROW-NAME"}] for n in CHILDREN},
        )
        code, text = self.run_check(site)
        self.assertEqual(code, 0)
        self.assertIn(f"child    empty    {CHILDREN[1]}", text.splitlines())
        self.assertNotIn("SECRET-ROW-NAME", text)

    def test_a_key_role_with_level_1_on_the_proposal_is_visible_and_exits_1(self):
        lister = FakeSite()
        lister.permissions.append({"role": "Stock User", "permlevel": 1})
        code, text = self.run_check(FakeSite(), lister)
        self.assertEqual(code, 1)
        self.assertIn("level1   visible  Payment Proposal, key roles", text)
        self.assertNotIn("Stock User", text)

    def test_level_1_for_other_roles_only_is_refused(self):
        _code, text = self.run_check(FakeSite())
        self.assertIn("level1   refused  Payment Proposal, key roles", text)

    def test_an_empty_leave_ledger_entry_exits_1_as_any_line_but_file(self):
        lister = FakeSite(
            modules={
                "Payroll": PAYROLL_DOCTYPES,
                "HR": HR_DOCTYPES + ["Leave Ledger Entry"],
                "BI Payroll": BI_PAYROLL_DOCTYPES,
            }
        )
        probed = FakeSite(
            readable={"Leave Ledger Entry"}, rows={"Leave Ledger Entry": False}
        )
        out = io.StringIO()
        self.assertEqual(akc.check(lister, probed, out), 1)
        self.assertIn("empty    Leave Ledger Entry", out.getvalue())

    def test_a_report_is_asked_through_get_script_and_run_is_never_called(self):
        probed = FakeSite(open_reports=REPORTS)
        self.run_check(probed)
        self.assertEqual(
            [m for m in probed.methods if m == GET_SCRIPT], [GET_SCRIPT] * len(REPORTS)
        )
        self.assertFalse(any(m.endswith(".run") for m in probed.methods))

    def test_every_method_gets_a_line_and_is_refused_by_the_gate(self):
        code, text = self.run_check(FakeSite())
        self.assertEqual(code, 0)
        self.assertIn(f"method   refused  {EMPLOYEE_QUERY}", text.splitlines())
        self.assertIn(
            f"method   refused  {SEARCH_LINK} (query employee_query)", text.splitlines()
        )

    def test_a_method_the_gate_lets_through_is_visible_and_exits_1(self):
        for method in (EMPLOYEE_QUERY, SEARCH_LINK):
            with self.subTest(method=method):
                code, text = self.run_check(FakeSite(open_methods={method}))
                self.assertEqual(code, 1)
                self.assertIn(f"method   visible  {method}", text)
                self.assertIn("1 visible, 20 refused", text)
                self.assertNotIn("SECRET-EMPLOYEE", text)

    def test_search_link_asks_for_one_row_through_the_employee_query(self):
        probed = FakeSite()
        self.run_check(probed)
        link = probed.method_args[SEARCH_LINK]
        self.assertEqual(link["query"], EMPLOYEE_QUERY)
        self.assertEqual(link["page_length"], 1)
        self.assertEqual(probed.method_args[EMPLOYEE_QUERY]["page_len"], 1)

    def test_only_the_reads_are_asked_never_a_method_that_writes(self):
        probed = FakeSite()
        self.run_check(probed)
        self.assertEqual(
            sorted(set(probed.methods)),
            sorted([EMPLOYEE_QUERY, SEARCH_LINK, GET_SCRIPT]),
        )
        self.assertFalse(any("create_payment_proposal" in m for m in probed.methods))

    def test_no_probed_method_is_on_the_gates_list(self):
        """A listed method answers by design; a probe of one would be visible on every site."""
        for method, _args, _note in akc.METHODS:
            with self.subTest(method=method):
                self.assertNotIn(method, api_gate.ALLOWED_METHODS)

    def test_another_http_error_on_a_method_is_not_read_as_refused(self):
        class Down(FakeSite):
            def call(self, method, **args):
                if method == EMPLOYEE_QUERY:
                    raise im.ErpError(500, "POST /method/" + method)
                return super().call(method, **args)

        with self.assertRaises(im.ErpError):
            akc.check(FakeSite(), Down(), io.StringIO())

    def test_another_http_error_on_a_report_is_not_read_as_refused(self):
        class Down(FakeSite):
            def call(self, method, **args):
                raise im.ErpError(500, "POST /method/" + method)

        with self.assertRaises(im.ErpError):
            akc.check(FakeSite(), Down(), io.StringIO())

    def test_another_http_error_is_not_read_as_refused(self):
        class Down(FakeSite):
            def list(self, doctype, *args, **kwargs):
                if doctype == "Salary Slip":
                    raise im.ErpError(500, "GET /resource/Salary Slip")
                return super().list(doctype, *args, **kwargs)

        with self.assertRaises(im.ErpError):
            akc.check(FakeSite(), Down(), io.StringIO())

    def test_a_module_that_holds_doctypes_and_lists_none_stops_the_check(self):
        """A renamed module would otherwise pass: nothing probed, nothing visible, exit 0."""
        lister = FakeSite(modules={"Payroll": PAYROLL_DOCTYPES, "HR": HR_DOCTYPES})
        with self.assertRaisesRegex(ValueError, "BI Payroll"):
            akc.check(lister, FakeSite(), io.StringIO())

    def test_the_hrms_modules_without_doctypes_do_not_stop_it(self):
        """Tax and Benefits and the others list none today; the fake's three modules are the whole list."""
        self.assertEqual(self.run_check(FakeSite())[0], 0)

    def main_with(self, lister, probed):
        """main() on a temp key file; the agent key is whatever im.TOKEN_FILE names."""
        out, err = io.StringIO(), io.StringIO()

        def from_file(path):
            return lister if path == "/agent/.erpnext-api" else probed

        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, ".erpnext-api-mcp")
            with mock.patch.object(
                im, "TOKEN_FILE", "/agent/.erpnext-api"
            ), mock.patch.object(
                im.Erp, "from_file", side_effect=from_file
            ), mock.patch(
                "sys.stdout", out
            ), mock.patch(
                "sys.stderr", err
            ):
                code = akc.main([key])
        return code, out.getvalue(), err.getvalue()

    def test_main_lists_with_the_agent_key_and_exits_with_the_verdict(self):
        code, text, _err = self.main_with(FakeSite(), FakeSite(readable={"Employee"}))
        self.assertEqual(code, 1)
        self.assertIn("visible  Employee", text)

    def test_main_reports_a_failed_call_as_exit_2(self):
        class Down(FakeSite):
            def list(self, doctype, *args, **kwargs):
                raise im.ErpError("network", "GET /resource/" + doctype)

        code, _text, err = self.main_with(Down(), Down())
        self.assertEqual(code, 2)
        self.assertIn("incomplete", err)

    def test_an_unreadable_or_broken_key_file_is_exit_2_not_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken")
            with open(broken, "w") as f:
                f.write("url=http://127.0.0.1:1/api/\n")
            err = io.StringIO()
            with mock.patch.object(im, "TOKEN_FILE", broken), mock.patch(
                "sys.stderr", err
            ):
                self.assertEqual(akc.main([broken]), 2)
                self.assertEqual(akc.main([os.path.join(tmp, "missing")]), 2)
        self.assertIn("incomplete", err.getvalue())


class ThroughTheRealErp(unittest.TestCase):
    """probe() through im.Erp itself: Frappe's 403 as HTTP, the row limit in the URL."""

    def erp(self):
        return im.Erp("http://127.0.0.1:1/api", "invented-key", "invented-secret")

    def test_a_403_response_is_refused(self):
        body = io.BytesIO(json.dumps({"exc_type": "PermissionError"}).encode())
        refusal = urllib.error.HTTPError(
            "http://x", 403, "FORBIDDEN", email.message.Message(), body
        )
        self.addCleanup(refusal.close)
        with mock.patch("urllib.request.urlopen", side_effect=refusal):
            self.assertEqual(akc.probe(self.erp(), "Salary Slip", []), "refused")

    def test_a_403_response_on_a_method_is_refused(self):
        body = io.BytesIO(json.dumps({"exc_type": "PermissionError"}).encode())
        refusal = urllib.error.HTTPError(
            "http://x", 403, "FORBIDDEN", email.message.Message(), body
        )
        self.addCleanup(refusal.close)
        with mock.patch("urllib.request.urlopen", side_effect=refusal):
            self.assertEqual(
                akc.probe_method(self.erp(), EMPLOYEE_QUERY, doctype="Employee"),
                "refused",
            )

    def urls_of_a_list(self, call):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        with mock.patch(
            "urllib.request.urlopen",
            return_value=Response(json.dumps({"data": []}).encode()),
        ) as urlopen:
            call(self.erp())
        return urlopen.call_args[0][0].full_url

    def test_the_parent_doctype_reaches_the_url_only_when_given(self):
        child = self.urls_of_a_list(
            lambda erp: akc.probe_child(
                erp, "Payment Proposal Expense", "Payment Proposal", ("employee",)
            )
        )
        self.assertIn("parent=Payment+Proposal&", child + "&")
        plain = self.urls_of_a_list(lambda erp: erp.list("Customer"))
        self.assertNotIn("parent=", plain)

    def test_the_limit_reaches_the_url_and_the_default_stays(self):
        one = self.urls_of_a_list(lambda erp: akc.probe(erp, "Employee", []))
        self.assertIn("limit_page_length=1&", one + "&")
        other = self.urls_of_a_list(lambda erp: erp.list("Customer"))
        self.assertIn("limit_page_length=100000", other)


if __name__ == "__main__":
    unittest.main()
