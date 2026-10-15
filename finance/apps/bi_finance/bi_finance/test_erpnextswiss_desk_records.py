"""The erpnextswiss desk rows patch: which rows it deletes, in what order, and that a rerun is silent.

Stdlib only, with a stub frappe that records the calls and keeps the rows in memory. Invented rows are the public
erpnextswiss names and bi_finance's own Treasury rows; no company data.

    python3 -m unittest bi_finance.test_erpnextswiss_desk_records
"""

import contextlib
import io
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))

SHIPPED = [
    "Schweizer Buchhaltung",
    "Zahlungsverkehr",
    "QR-Rechnung & E-Rechnung",
    "Schweizer MwSt",
    "Schweiz-Einstellungen",
]


class Frappe(types.ModuleType):
    """The three doctypes as rows; get_all matches the filters, delete_doc removes a row and records the call."""

    def __init__(self):
        super().__init__("frappe")
        self.rows = {
            "Desktop Icon": [
                {"name": "Schweizer Buchhaltung", "app": "erpnextswiss"},
                {"name": "Treasury", "app": "bi_finance"},
            ],
            "Workspace Sidebar": [
                {"name": "Schweizer Buchhaltung", "app": "erpnextswiss"},
                {"name": "Treasury", "app": "bi_finance"},
            ],
            "Workspace": [{"name": name, "module": "ERPNextSwiss"} for name in SHIPPED]
            + [{"name": "Treasury", "module": "BI Finance"}],
        }
        self.get_all_calls = []
        self.delete_calls = []
        self.cache_clears = 0

    def get_all(self, doctype, filters=None, pluck=None):
        self.get_all_calls.append((doctype, filters, pluck))
        found = [
            r
            for r in self.rows[doctype]
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]
        return [r[pluck] for r in found] if pluck else found

    def delete_doc(self, doctype, name, **kwargs):
        self.delete_calls.append((doctype, name, kwargs))
        self.rows[doctype] = [r for r in self.rows[doctype] if r["name"] != name]

    def clear_cache(self):
        self.cache_clears += 1


# the patch reads frappe at import; the stub stands in for it only while the module loads
with mock.patch.dict(sys.modules, {"frappe": Frappe()}):
    from bi_finance.patches import erpnextswiss_desk_records as patch


class DeskRecords(unittest.TestCase):
    def setUp(self):
        self.frappe = Frappe()
        self.addCleanup(setattr, patch, "frappe", patch.frappe)
        patch.frappe = self.frappe

    def run_patch(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            patch.execute()
        return out.getvalue()

    def test_the_filters_name_only_the_erpnextswiss_rows_and_the_erpnextswiss_module(
        self,
    ):
        self.run_patch()
        self.assertEqual(
            self.frappe.get_all_calls,
            [
                ("Desktop Icon", {"app": "erpnextswiss"}, "name"),
                ("Workspace Sidebar", {"app": "erpnextswiss"}, "name"),
                ("Workspace", {"module": "ERPNextSwiss"}, "name"),
            ],
        )

    def test_the_rows_go_in_order_the_icon_and_the_sidebar_before_the_workspaces_and_with_force(
        self,
    ):
        self.run_patch()
        doctypes = [c[0] for c in self.frappe.delete_calls]
        self.assertEqual(
            doctypes, ["Desktop Icon"] + ["Workspace Sidebar"] + ["Workspace"] * 5
        )
        self.assertEqual([c[1] for c in self.frappe.delete_calls[2:]], SHIPPED)
        for doctype, name, kwargs in self.frappe.delete_calls:
            self.assertEqual(
                kwargs,
                {"ignore_permissions": True, "force": True, "ignore_missing": True},
                name,
            )

    def test_bi_finance_rows_are_left_alone(self):
        self.run_patch()
        self.assertEqual(
            self.frappe.rows["Desktop Icon"],
            [{"name": "Treasury", "app": "bi_finance"}],
        )
        self.assertEqual(
            self.frappe.rows["Workspace Sidebar"],
            [{"name": "Treasury", "app": "bi_finance"}],
        )
        self.assertEqual(
            self.frappe.rows["Workspace"],
            [{"name": "Treasury", "module": "BI Finance"}],
        )

    def test_the_first_run_prints_one_line_with_the_three_counts(self):
        out = self.run_patch()
        self.assertEqual(
            out,
            "erpnextswiss_desk_records: deleted 1 Desktop Icon, 1 Workspace Sidebar, 5 Workspace\n",
        )

    def test_a_rerun_deletes_nothing_and_prints_nothing(self):
        self.run_patch()
        self.frappe.delete_calls.clear()
        self.assertEqual(self.run_patch(), "")
        self.assertEqual(self.frappe.delete_calls, [])

    def test_the_caches_are_cleared_on_every_run(self):
        self.run_patch()
        self.run_patch()
        self.assertEqual(self.frappe.cache_clears, 2)


class Listed(unittest.TestCase):
    def test_the_patch_is_listed_after_the_model_sync(self):
        with open(os.path.join(HERE, "patches.txt"), encoding="utf-8") as f:
            post = f.read().split("[post_model_sync]")[1]
        self.assertIn("\nbi_finance.patches.erpnextswiss_desk_records\n", post)


if __name__ == "__main__":
    unittest.main()
