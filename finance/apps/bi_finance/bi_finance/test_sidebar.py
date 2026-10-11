"""The shipped Sidebar document for module BI Finance: what the desk's Apps-mode menu reads.

Frappe v16 computes a module's left-hand menu from what the module holds unless the app
ships a `Sidebar` document (`<app>/<module>/sidebar/<name>/<name>.json`). The document is
what puts VAT Declaration and Payment Proposal — erpnextswiss's doctypes — into the BI
Finance menu, which the computed sidebar cannot show. Offline, stdlib only:

    python3 -m unittest bi_finance.test_sidebar

Ownership: `is_default_module` on an item claims the entity for this shell, so it is set
only on bi_finance's own doctypes and reports — never on the four rows that other apps
ship, whose shells keep them (VAT Declaration and Payment Proposal open in the Schweizer
Buchhaltung shell, the two bank rows in Accounts).
"""

import json
import os
import unittest

from bi_finance.test_layout import PACKAGE, modules, scrub

SIDEBAR = os.path.join(PACKAGE, scrub(modules()[0]), "sidebar", "bi_finance", "bi_finance.json")

# what the computed sidebar shows today: none of it may disappear
KEEP = (
    "Treasury",
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

# the rows another app ships: present here, never claimed (their documents open in their
# own shell, not this one)
FOREIGN = {
    "VAT Declaration": "DocType",
    "Payment Proposal": "DocType",
    "Bank Transaction": "DocType",
    "Bank Reconciliation Tool": "DocType",
}


def load():
    with open(SIDEBAR, encoding="utf-8") as f:
        return json.load(f)


def own_names(folder):
    home = os.path.join(PACKAGE, scrub(modules()[0]), folder)
    return {name for name in os.listdir(home) if os.path.isdir(os.path.join(home, name))}


class Sidebar(unittest.TestCase):
    def setUp(self):
        self.doc = load()
        self.links = [item for item in self.doc["items"] if item["type"] == "Link"]

    def test_the_document_is_the_module_sidebar(self):
        self.assertEqual(self.doc["doctype"], "Sidebar")
        self.assertEqual(self.doc["name"], "BI Finance")
        self.assertEqual(self.doc["title"], "BI Finance")
        self.assertEqual(self.doc["module"], "BI Finance")
        self.assertEqual(self.doc["app"], "bi_finance")

    def test_nothing_the_computed_sidebar_shows_today_is_lost(self):
        labels = {link["label"] for link in self.links}
        for label in KEEP:
            self.assertIn(label, labels)

    def test_the_erpnextswiss_items_are_present_and_unclaimed(self):
        by_label = {link["label"]: link for link in self.links}
        for label, link_type in FOREIGN.items():
            self.assertEqual(by_label[label]["link_type"], link_type)
            self.assertFalse(by_label[label].get("is_default_module"))

    def test_every_doctype_and_report_row_names_something_that_ships(self):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            if link["link_type"] not in ("DocType", "Report"):
                continue
            if link["label"] in FOREIGN:
                self.assertNotIn(scrub(link["link_to"]), own, link["label"])
            else:
                self.assertIn(scrub(link["link_to"]), own, link["label"])

    def test_ownership_is_claimed_only_on_the_app_s_own_rows(self):
        own = own_names("doctype") | own_names("report")
        for link in self.links:
            claimed = bool(link.get("is_default_module"))
            should = link["link_type"] in ("DocType", "Report") and scrub(link["link_to"]) in own
            self.assertEqual(claimed, should, link["label"])

    def test_the_workspace_row_names_the_shipped_workspace(self):
        workspaces = own_names("workspace")
        for link in self.links:
            if link["link_type"] == "Workspace":
                self.assertIn(scrub(link["link_to"]), workspaces, link["label"])

    def test_the_sections_group_the_menu_in_the_asked_order(self):
        sections = [item["label"] for item in self.doc["items"] if item["type"] == "Section Break"]
        self.assertEqual(sections, ["Banking", "Payments", "MWST", "Cash forecast"])
        self.assertEqual(self.doc["items"][0]["label"], "Treasury")
        for item in self.doc["items"]:
            if item["type"] == "Section Break":
                self.assertEqual(item["indent"], 1)
            else:
                self.assertEqual(item["indent"], 0)


if __name__ == "__main__":
    unittest.main()
