"""The area cards on the Treasury workspace: their links, their order on the page, the shortcuts they repeat. Stdlib only.

    python3 -m unittest bi_finance.test_treasury_card

The stamp of treasury.json is checked by test_layout; this file checks what the cards hold. The groups and their
order are the module sidebar's (sidebar/bi_finance/bi_finance.json), so a change there is a change here.
"""

import json
import os
import unittest
from collections import Counter

from bi_finance.test_layout import PACKAGE

WORKSPACE = os.path.join(PACKAGE, "bi_finance", "workspace", "treasury", "treasury.json")

# the cards in the order they sit on the page, with their links in the order Benchi uses them
AREAS = [
    ("Banking", [
        "Bank Statement Upload",
        "Bank Transaction",
        "Bank Reconciliation Tool",
        "Wise Settings",
        "PayPal Settings",
        "Bank Matching Settings",
    ]),
    ("Payments", ["Payment Proposal"]),
    ("MWST", ["VAT Declaration", "MWST pro Quartal"]),
    ("Cash forecast", [
        "Cash Position",
        "Cash Flow Forecast",
        "Cash Flow Forecast Lines",
        "Cash Conversion Cycle",
        "Cash Forecast Settings",
    ]),
    ("Payroll", ["Payroll Swiss Settings", "QST Tariff", "Payroll Hand-over"]),
]

# the shortcuts the page keeps, in order; a shortcut repeats a card link only for the two reports named here
SHORTCUTS = ["Bank Transactions", "Unreconciled", "Cash Flow Forecast", "MWST pro Quartal"]
REPEATED = {"Cash Flow Forecast", "MWST pro Quartal"}


def load():
    with open(WORKSPACE, encoding="utf-8") as f:
        return json.load(f)


class Cards(unittest.TestCase):
    def setUp(self):
        self.doc = load()

    def card_links(self, name):
        links = self.doc["links"]
        start = next(i for i, link in enumerate(links) if link["type"] == "Card Break" and link["label"] == name)
        out = []
        for link in links[start + 1:]:
            if link["type"] == "Card Break":
                break
            out.append(link)
        return out

    def card_breaks(self):
        return [link["label"] for link in self.doc["links"] if link["type"] == "Card Break"]

    def test_the_cards_are_in_the_sidebar_order_with_their_links_in_order(self):
        self.assertEqual(self.card_breaks(), [name for name, _ in AREAS])
        for name, labels in AREAS:
            self.assertEqual([link["label"] for link in self.card_links(name)], labels, name)

    def test_each_card_counts_its_links(self):
        for name, _ in AREAS:
            self.assertEqual(
                next(link for link in self.doc["links"] if link["label"] == name)["link_count"],
                len(self.card_links(name)),
                name,
            )

    def test_each_link_names_its_target(self):
        for name, _ in AREAS:
            for link in self.card_links(name):
                self.assertTrue(link["link_to"], link["label"])
                self.assertIn(link["link_type"], ("DocType", "Report"), link["label"])

    def test_the_cards_sit_on_the_page_after_the_charts_in_the_asked_order(self):
        content = json.loads(self.doc["content"])
        cards = [block["data"]["card_name"] for block in content if block["type"] == "card"]
        self.assertEqual(cards, [name for name, _ in AREAS])
        last_chart = max(i for i, block in enumerate(content) if block["type"] == "chart")
        first_card = min(i for i, block in enumerate(content) if block["type"] == "card")
        self.assertLess(last_chart, first_card)

    def test_the_page_block_ids_are_unique(self):
        ids = [block["id"] for block in json.loads(self.doc["content"])]
        self.assertEqual([i for i, n in Counter(ids).items() if n > 1], [])

    def test_a_report_link_names_a_report_that_exists_and_opens_as_a_query_report(self):
        # the link_to is the report's own name ("Payroll Hand-over"), not the folder or a label
        names = set()
        for app in ("bi_finance", "bi_payroll"):
            root = os.path.join(PACKAGE, "..", "..", app, app, app, "report")
            for folder in (os.listdir(root) if os.path.isdir(root) else []):
                if not os.path.isdir(os.path.join(root, folder)):
                    continue
                for file in os.listdir(os.path.join(root, folder)):
                    if file.endswith(".json"):
                        with open(os.path.join(root, folder, file), encoding="utf-8") as f:
                            names.add(json.load(f)["name"])
        for name, _ in AREAS:
            for link in self.card_links(name):
                if link["link_type"] == "Report":
                    self.assertIn(link["link_to"], names, link["label"])
                    self.assertEqual(link["is_query_report"], 1, link["label"])

    def test_bank_statement_upload_is_in_one_place_only(self):
        shortcuts = [s["label"] for s in self.doc["shortcuts"]]
        links = [link["label"] for link in self.doc["links"] if link["type"] == "Link"]
        self.assertEqual(shortcuts.count("Bank Statement Upload") + links.count("Bank Statement Upload"), 1)

    def test_the_shortcuts_are_the_four_the_page_keeps(self):
        self.assertEqual([s["label"] for s in self.doc["shortcuts"]], SHORTCUTS)

    def test_a_shortcut_repeats_a_card_link_only_where_the_page_keeps_it(self):
        shortcuts = {s["label"] for s in self.doc["shortcuts"]}
        card = {link["label"] for name, _ in AREAS for link in self.card_links(name)}
        self.assertEqual(shortcuts & card, REPEATED)

    def test_the_dropped_shortcuts_stay_on_their_cards(self):
        shortcuts = {s["label"] for s in self.doc["shortcuts"]}
        for label in ("Bank Reconciliation Tool", "Cash Forecast Settings"):
            self.assertNotIn(label, shortcuts)
            self.assertIn(label, [link["label"] for name, _ in AREAS for link in self.card_links(name)])


if __name__ == "__main__":
    unittest.main()
