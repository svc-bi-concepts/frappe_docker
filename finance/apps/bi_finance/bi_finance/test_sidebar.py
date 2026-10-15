"""The shipped Sidebar document for module BI Finance: what the desk's Apps-mode menu reads.

Frappe v16 computes a module's left-hand menu from what the module holds unless the app
ships a `Sidebar` document (`<app>/<module>/sidebar/<name>/<name>.json`). The document is
what puts VAT Declaration and Payment Proposal — erpnextswiss's doctypes — into the BI
Finance menu, which the computed sidebar cannot show. Offline, stdlib only:

    python3 -m unittest bi_finance.test_sidebar

Ownership: `is_default_module` on an item claims the entity for this shell. It is set on
bi_finance's own doctypes and reports, and on the two erpnextswiss documents Benchi works in
every day (VAT Declaration, Payment Proposal), and on ERPNextSwiss Settings (planning_days of the
payment run, under Setup): without the claim a bare route to them (a workspace card, a search
result, the Add button of a bare list) opens erpnextswiss's 60-item computed shell. It is never set on ERPNext's own bank rows (Bank Transaction,
Bank Reconciliation Tool, Bank Account): ERPNext's shell keeps them, and the two bank pages
resolve here by being listed.
"""

import json
import os
import unittest

from bi_finance import hooks
from bi_finance.test_layout import PACKAGE, modules, scrub

SIDEBAR = os.path.join(
    PACKAGE, scrub(modules()[0]), "sidebar", "treasury", "treasury.json"
)
DOCK = os.path.join(PACKAGE, "dock", "bi_finance", "bi_finance.json")

# what the computed sidebar shows today: none of it may disappear
KEEP = (
    "Home",  # the Treasury row, relabelled Home (ERPNext's first row)
    "Wise Settings",
    "PayPal Settings",
    "Bank Statement Upload",
    "Cash Forecast Settings",
    "Bank Matching Settings",
    "Cash Position",
    "Cash Conversion Cycle",
    "Cash Flow Forecast",
    "Cash Flow Forecast Lines",
    "MWST pro Quartal",
)

# the three erpnextswiss documents Benchi works in: present here and claimed, so a bare route
# to them opens in this shell (the reason is in the module docstring)
CLAIMED = {
    "VAT Declaration": "DocType",
    "Payment Proposal": "DocType",
    "ERPNextSwiss Settings": "DocType",
}

# ERPNext's own bank rows: present here, never claimed (ERPNext's shell keeps them)
ERPNEXT = {
    "Bank Transaction": "DocType",
    "Bank Reconciliation Tool": "DocType",
    "Bank Account": "DocType",
}

FOREIGN = {
    **CLAIMED,
    **ERPNEXT,
}  # rows another app ships: their names are not this app's to ship


def load():
    with open(SIDEBAR, encoding="utf-8") as f:
        return json.load(f)


def own_names(folder):
    home = os.path.join(PACKAGE, scrub(modules()[0]), folder)
    return {
        name for name in os.listdir(home) if os.path.isdir(os.path.join(home, name))
    }


class Sidebar(unittest.TestCase):
    def setUp(self):
        self.doc = load()
        self.links = [item for item in self.doc["items"] if item["type"] == "Link"]

    def test_the_document_is_the_module_sidebar(self):
        self.assertEqual(self.doc["doctype"], "Sidebar")
        # a Sidebar is named by its title (autoname field:title): the name is what the shell header shows
        self.assertEqual(self.doc["name"], "Treasury")
        # the title is what the shell header and the rail tooltip show, next to Accounts and Selling
        self.assertEqual(self.doc["title"], "Treasury")
        self.assertEqual(self.doc["module"], "BI Finance")
        self.assertEqual(self.doc["app"], "bi_finance")

    def test_nothing_the_computed_sidebar_shows_today_is_lost(self):
        labels = {link["label"] for link in self.links}
        for label in KEEP:
            self.assertIn(label, labels)

    def test_the_erpnextswiss_documents_are_claimed_and_the_erpnext_rows_are_not(self):
        self.assertEqual(len(CLAIMED), 3)
        by_label = {link["label"]: link for link in self.links}
        for label, link_type in CLAIMED.items():
            self.assertEqual(by_label[label]["link_type"], link_type)
            self.assertTrue(by_label[label].get("is_default_module"), label)
        for label, link_type in ERPNEXT.items():
            self.assertEqual(by_label[label]["link_type"], link_type)
            self.assertFalse(by_label[label].get("is_default_module"), label)

    def test_every_doctype_and_report_row_names_something_that_ships(self):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            if link["link_type"] not in ("DocType", "Report"):
                continue
            if link["label"] in FOREIGN:
                self.assertNotIn(scrub(link["link_to"]), own, link["label"])
            else:
                self.assertIn(scrub(link["link_to"]), own, link["label"])

    def test_ownership_is_claimed_on_the_app_s_own_rows_and_the_two_erpnextswiss_documents(
        self,
    ):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            claimed = bool(link.get("is_default_module"))
            should = link["link_type"] in ("DocType", "Report") and (
                scrub(link["link_to"]) in own or link["label"] in CLAIMED
            )
            self.assertEqual(claimed, should, link["label"])

    def test_the_dock_mounts_the_rail_entry_on_erpnext_and_opens_the_module_sidebar(
        self,
    ):
        # BI Finance is part of ERPNext: its one rail entry sits on ERPNext's rail (frappe's companion mount),
        # and the entry opens the Sidebar above
        with open(DOCK, encoding="utf-8") as f:
            dock = json.load(f)
        self.assertEqual(dock["doctype"], "Dock")
        self.assertEqual(dock["name"], "bi_finance")
        self.assertEqual(dock["app"], "bi_finance")
        self.assertEqual(dock["mount_on"], "erpnext")
        self.assertEqual(dock["standard"], 1)
        self.assertEqual(
            [
                (item["link_type"], item["link_to"], item["title"])
                for item in dock["items"]
            ],
            [("Sidebar", "Treasury", "Treasury")],
        )
        self.assertEqual(self.doc["name"], dock["items"][0]["link_to"])

    def test_bi_finance_has_no_apps_screen_tile_and_no_logo(self):
        # a companion of ERPNext takes no tile on the apps screen or the switcher
        self.assertFalse(hasattr(hooks, "add_to_apps_screen"))
        self.assertFalse(
            os.path.exists(os.path.join(PACKAGE, "public", "images", "bi_finance.svg"))
        )

    def test_the_workspace_row_names_the_shipped_workspace(self):
        workspaces = own_names("workspace")
        for link in self.links:
            if link["link_type"] == "Workspace":
                self.assertIn(scrub(link["link_to"]), workspaces, link["label"])

    def test_the_menu_reads_home_then_the_daily_documents_then_the_sections(self):
        # ERPNext's arrangement: Home, the daily documents at top level with icons, then the sections
        items = self.doc["items"]
        self.assertEqual(items[0]["label"], "Home")
        self.assertEqual(items[0]["link_type"], "Workspace")
        first_section = next(
            i for i, item in enumerate(items) if item["type"] == "Section Break"
        )
        daily = [item["label"] for item in items[1:first_section]]
        self.assertEqual(
            daily,
            [
                "Bank Transaction",
                "Bank Statement Upload",
                "Payment Proposal",
                "VAT Declaration",
            ],
        )
        for item in items[1:first_section]:
            self.assertEqual(item["child"], 0, item["label"])
            self.assertTrue(item.get("icon"), item["label"])
        sections = [item["label"] for item in items if item["type"] == "Section Break"]
        self.assertEqual(sections, ["Reports", "Banking", "Setup"])
        opened = {
            item["label"]: item["keep_closed"]
            for item in items
            if item["type"] == "Section Break"
        }
        self.assertEqual(opened, {"Reports": 0, "Banking": 1, "Setup": 1})
        for item in items:
            if item["type"] == "Section Break":
                self.assertEqual(item["indent"], 1)
            else:
                self.assertEqual(item["indent"], 0)


if __name__ == "__main__":
    unittest.main()
