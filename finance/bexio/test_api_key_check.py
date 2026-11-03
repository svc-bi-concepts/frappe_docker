"""Offline tests for api_key_check.py: a fake ERPNext that refuses as Frappe does. Invented doctypes and rows only.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import email.message
import io
import json
import os
import tempfile
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
ON_SALARY_SLIP = "File (attached_to_doctype = Salary Slip)"
ON_EMPLOYEE = "File (attached_to_doctype = Employee)"
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
    """list() as Frappe answers it: 403 for a doctype the user cannot read, else the rows (one per call at most)."""

    def __init__(self, readable=(), rows=None, modules=None):
        self.readable = set(readable)
        self.rows = rows or {}
        self.calls = []
        self.modules = modules or {
            "Payroll": PAYROLL_DOCTYPES,
            "HR": HR_DOCTYPES,
            "BI Payroll": BI_PAYROLL_DOCTYPES,
        }

    def list(self, doctype, filters=None, fields=("name",), limit=100000):
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


class ApiKeyCheck(unittest.TestCase):
    def run_check(self, probed):
        out = io.StringIO()
        code = akc.check(FakeSite(), probed, out)
        return code, out.getvalue()

    def test_every_line_refused_exits_0(self):
        code, text = self.run_check(FakeSite())
        self.assertEqual(code, 0)
        self.assertIn("refused  Salary Slip", text)
        self.assertIn("total 11: 0 visible, 11 refused, 0 empty", text)

    def test_empty_file_lines_exit_0(self):
        probed = FakeSite(readable={"File"}, rows={"File": False})
        code, text = self.run_check(probed)
        self.assertEqual(code, 0)
        self.assertIn(f"empty    {ON_SALARY_SLIP}", text)
        self.assertIn(f"empty    {ON_EMPLOYEE}", text)
        self.assertIn("total 11: 0 visible, 9 refused, 2 empty", text)

    def test_a_visible_file_line_exits_1(self):
        code, text = self.run_check(FakeSite(readable={"File"}))
        self.assertEqual(code, 1)
        self.assertIn(f"visible  {ON_EMPLOYEE}", text)

    def test_a_module_doctype_the_key_may_list_with_no_rows_exits_1(self):
        probed = FakeSite(readable={"Leave Type"}, rows={"Leave Type": False})
        code, text = self.run_check(probed)
        self.assertEqual(code, 1)
        self.assertIn("empty    Leave Type", text)
        self.assertIn("total 11: 0 visible, 10 refused, 1 empty", text)

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
        self.assertIn("1 visible, 10 refused", text)

    def test_every_doctype_gets_a_line_in_order(self):
        _code, text = self.run_check(FakeSite())
        self.assertEqual(
            [line.split(" ", 1)[1].strip() for line in text.splitlines()[:-1]],
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
        _code, text = self.run_check(FakeSite(readable=readable))
        self.assertNotIn("SECRET-ROW-NAME", text)

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

    def test_the_limit_reaches_the_url_and_the_default_stays(self):
        one = self.urls_of_a_list(lambda erp: akc.probe(erp, "Employee", []))
        self.assertIn("limit_page_length=1&", one + "&")
        other = self.urls_of_a_list(lambda erp: erp.list("Customer"))
        self.assertIn("limit_page_length=100000", other)


if __name__ == "__main__":
    unittest.main()
