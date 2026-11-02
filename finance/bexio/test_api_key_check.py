"""Offline tests for api_key_check.py: a fake ERPNext that refuses as Frappe does. Invented doctypes and rows only.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import os
import tempfile
import unittest
from unittest import mock

import api_key_check as akc
import import_master as im
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)

PAYROLL_DOCTYPES = ["Payroll Entry", "Salary Slip", "Salary Structure"]
HR_DOCTYPES = ["Appraisal Cycle", "Leave Type"]
BI_PAYROLL_DOCTYPES = ["Qst Code"]
ALL_PROBED = sorted(PAYROLL_DOCTYPES + HR_DOCTYPES + BI_PAYROLL_DOCTYPES) + [
    "Employee",
    "Version",
    "File",
]


class FakeSite:
    """list() as Frappe answers it: 403 for a doctype the user cannot read, else the rows (one per call at most)."""

    def __init__(self, readable=(), rows=None):
        self.readable = set(readable)
        self.rows = rows or {}
        self.calls = []

    def list(self, doctype, filters=None, fields=("name",), limit=100000):
        self.calls.append((doctype, filters, tuple(fields), limit))
        if doctype == "DocType":
            wanted = {m for _f, _op, ms in filters if _f == "module" for m in ms}
            assert wanted == {"Payroll", "HR", "BI Payroll"}, wanted
            return [
                {"name": n}
                for n in PAYROLL_DOCTYPES + HR_DOCTYPES + BI_PAYROLL_DOCTYPES
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

    def test_a_key_with_no_payroll_role_sees_nothing_and_exits_0(self):
        probed = FakeSite(readable={"Leave Type"}, rows={"Leave Type": False})
        code, text = self.run_check(probed)
        self.assertEqual(code, 0)
        self.assertIn("empty    Leave Type", text)
        self.assertIn("refused  Salary Slip", text)
        self.assertIn("total 9: 0 visible, 8 refused, 1 empty", text)

    def test_one_visible_doctype_exits_1_and_is_named(self):
        probed = FakeSite(readable={"Salary Slip"})
        code, text = self.run_check(probed)
        self.assertEqual(code, 1)
        self.assertIn("visible  Salary Slip", text)
        self.assertIn("1 visible, 8 refused", text)

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
        filters = {d: f for d, f, _fields, _l in probed.calls}
        self.assertEqual(filters["Version"], [["ref_doctype", "=", "Salary Slip"]])
        self.assertEqual(filters["File"], [["attached_to_doctype", "=", "Salary Slip"]])

    def test_the_doctype_list_leaves_out_single_and_child_doctypes(self):
        lister = FakeSite()
        akc.targets(lister)
        filters = lister.calls[0][1]
        self.assertIn(["issingle", "=", 0], filters)
        self.assertIn(["istable", "=", 0], filters)

    def test_no_row_and_no_name_is_printed(self):
        _code, text = self.run_check(FakeSite(readable=set(ALL_PROBED)))
        self.assertNotIn("SECRET-ROW-NAME", text)

    def test_another_http_error_is_not_read_as_refused(self):
        class Down(FakeSite):
            def list(self, doctype, *args, **kwargs):
                if doctype == "Salary Slip":
                    raise im.ErpError(500, "GET /resource/Salary Slip")
                return super().list(doctype, *args, **kwargs)

        with self.assertRaises(im.ErpError):
            akc.check(FakeSite(), Down(), io.StringIO())

    def test_main_reads_the_lister_key_beside_the_key_file_and_exits_with_the_verdict(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            for name in (".erpnext-api", ".erpnext-api-mcp"):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write("url=http://127.0.0.1:1/api/\napi_key=k\napi_secret=s\n")
            sites = {"lister": FakeSite(), "probed": FakeSite(readable={"Employee"})}

            def from_file(path):
                return sites["lister" if path.endswith(".erpnext-api") else "probed"]

            out = io.StringIO()
            with mock.patch.object(
                im.Erp, "from_file", side_effect=from_file
            ), mock.patch("sys.stdout", out):
                code = akc.main([os.path.join(tmp, ".erpnext-api-mcp")])
        self.assertEqual(code, 1)
        self.assertIn("visible  Employee", out.getvalue())

    def test_main_reports_a_failed_call_as_exit_2(self):
        class Down(FakeSite):
            def list(self, doctype, *args, **kwargs):
                raise im.ErpError("network", "GET /resource/" + doctype)

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, ".erpnext-api-mcp")
            with open(key, "w") as f:
                f.write("url=http://127.0.0.1:1/api/\napi_key=k\napi_secret=s\n")
            with mock.patch.object(
                im.Erp, "from_file", return_value=Down()
            ), mock.patch("sys.stderr", err):
                code = akc.main([key, "--lister-key", key])
        self.assertEqual(code, 2)
        self.assertIn("incomplete", err.getvalue())


if __name__ == "__main__":
    unittest.main()
