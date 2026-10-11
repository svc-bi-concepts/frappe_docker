"""The shipped Sidebar and Dock of bi_payroll: the Swiss Payroll rail on ERPNext's desk. Stdlib only.

    python3 -m unittest bi_payroll.test_sidebar

from this app's directory. The Sidebar is what the module's left-hand menu reads (sidebar/bi_payroll/bi_payroll.json,
in the module folder, as bi_finance's is); the Dock is what puts "Swiss Payroll" on ERPNext's rail (dock/bi_payroll/,
app-rooted, mount_on erpnext: one shell with the HR part, Benchi 2026-10-12). The app has no apps-screen tile.

Ownership: `is_default_module` claims an entity for this shell, so it is set only on bi_payroll's own documents and
reports. Frappe HR's documents (Payroll Entry, Salary Slip, Salary Register, Salary Structure, Salary Component) are
linked unclaimed: they open in this shell when reached from here and in their own shell when reached bare.
"""

import json
import os
import unittest

from bi_payroll import hooks
from bi_payroll.test_layout import PACKAGE, modules, scrub

MODULE = os.path.join(PACKAGE, scrub(modules()[0]))
SIDEBAR = os.path.join(MODULE, "sidebar", "bi_payroll", "bi_payroll.json")
DOCK = os.path.join(PACKAGE, "dock", "bi_payroll", "bi_payroll.json")

# the rows hrms ships: linked here, never claimed, and not ours to find in this app
HRMS = {
    "Payroll Entry": "DocType",
    "Salary Slip": "DocType",
    "Salary Register": "Report",
    "Salary Structure": "DocType",
    "Salary Component": "DocType",
}

# the sections in the order the menu shows them, and whether each opens
SECTIONS = [("Reports", 0), ("Setup", 1)]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def own_names(folder):
    home = os.path.join(MODULE, folder)
    if not os.path.isdir(home):
        return set()
    return {name for name in os.listdir(home) if os.path.isdir(os.path.join(home, name))}


class Sidebar(unittest.TestCase):
    def setUp(self):
        self.doc = load(SIDEBAR)
        self.links = [item for item in self.doc["items"] if item["type"] == "Link"]

    def test_the_document_is_the_module_sidebar_under_the_swiss_payroll_title(self):
        self.assertEqual(self.doc["doctype"], "Sidebar")
        self.assertEqual(self.doc["name"], "BI Payroll")
        self.assertEqual(self.doc["module"], "BI Payroll")
        self.assertEqual(self.doc["title"], "Swiss Payroll")
        self.assertEqual(self.doc["header_icon"], "wallet-duotone")
        self.assertEqual(self.doc["app"], "bi_payroll")

    def test_home_is_the_hrms_payroll_workspace(self):
        home = self.doc["items"][0]
        self.assertEqual((home["type"], home["link_type"], home["link_to"], home["label"]),
                         ("Link", "Workspace", "Payroll", "Home"))
        self.assertEqual(home["icon"], "house")
        self.assertEqual(home["indent"], 0)
        self.assertFalse(home.get("is_default_module"))
        # the workspace is hrms's: this app ships none
        self.assertNotIn("payroll", own_names("workspace"))

    def test_the_daily_documents_sit_at_the_top_level_with_icons_and_stay_unclaimed(self):
        top = [item for item in self.links if item["indent"] == 0 and item["link_type"] == "DocType"
               and item["label"] in ("Payroll Entry", "Salary Slip")]
        self.assertEqual([item["label"] for item in top], ["Payroll Entry", "Salary Slip"])
        for item in top:
            self.assertTrue(item.get("icon"), item["label"])
            self.assertFalse(item.get("is_default_module"), item["label"])

    def test_the_sections_are_reports_then_setup_and_open_or_close_as_asked(self):
        sections = [item for item in self.doc["items"] if item["type"] == "Section Break"]
        self.assertEqual([item["label"] for item in sections], [name for name, _ in SECTIONS])
        for item, (name, closed) in zip(sections, SECTIONS):
            self.assertEqual(item["indent"], 1, name)
            self.assertEqual(item["keep_closed"], closed, name)

    def test_the_rows_sit_under_their_section_in_the_asked_order(self):
        order = [(item["label"], item["type"]) for item in self.doc["items"]]
        reports = order.index(("Reports", "Section Break"))
        setup = order.index(("Setup", "Section Break"))
        self.assertEqual([label for label, kind in order[reports + 1:setup] if kind == "Link"],
                         ["Payroll Hand-over", "Salary Register"])
        self.assertEqual([label for label, kind in order[setup + 1:] if kind == "Link"],
                         ["Payroll Swiss Settings", "QST Tariff Code", "QST Tariff", "Salary Structure", "Salary Component"])

    def test_every_doctype_and_report_row_names_something_that_ships_or_is_hrms(self):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            if link["link_type"] not in ("DocType", "Report"):
                continue
            if link["label"] in HRMS:
                self.assertEqual(HRMS[link["label"]], link["link_type"], link["label"])
                self.assertNotIn(scrub(link["link_to"]), own, link["label"])
            else:
                self.assertIn(scrub(link["link_to"]), own, link["label"])

    def test_ownership_is_claimed_only_on_the_app_s_own_rows(self):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            claimed = bool(link.get("is_default_module"))
            should = link["link_type"] in ("DocType", "Report") and scrub(link["link_to"]) in own
            self.assertEqual(claimed, should, link["label"])

    def test_the_hr_rows_are_linked_to_their_own_names(self):
        by_label = {link["label"]: link for link in self.links}
        for label, link_type in HRMS.items():
            self.assertEqual(by_label[label]["link_to"], label)
            self.assertEqual(by_label[label]["link_type"], link_type)


class Dock(unittest.TestCase):
    def setUp(self):
        self.doc = load(SIDEBAR)
        self.dock = load(DOCK)

    def test_the_dock_is_bi_payroll_s_own_and_mounts_on_erpnext(self):
        self.assertEqual(self.dock["doctype"], "Dock")
        self.assertEqual(self.dock["name"], "bi_payroll")
        self.assertEqual(self.dock["app"], "bi_payroll")
        self.assertEqual(self.dock["standard"], 1)
        self.assertEqual(self.dock["mount_on"], "erpnext")

    def test_the_dock_rail_opens_the_module_sidebar_that_ships(self):
        items = self.dock["items"]
        self.assertEqual([(item["link_type"], item["link_to"]) for item in items], [("Sidebar", "BI Payroll")])
        self.assertEqual(items[0]["title"], "Swiss Payroll")
        self.assertEqual(self.doc["name"], items[0]["link_to"])

    def test_the_app_has_no_apps_screen_tile(self):
        self.assertFalse(hasattr(hooks, "add_to_apps_screen"))


class Layout(unittest.TestCase):
    def test_the_sidebar_and_the_dock_are_in_the_stamp_manifest(self):
        with open(os.path.join(PACKAGE, "synced_manifest.json"), encoding="utf-8") as f:
            recorded = json.load(f)
        self.assertIn("bi_payroll/sidebar/bi_payroll/bi_payroll.json", recorded)
        self.assertIn("dock/bi_payroll/bi_payroll.json", recorded)


if __name__ == "__main__":
    unittest.main()
