"""The Switzerland card on the Treasury workspace: its links, its place in the page, no duplicate entry. Stdlib only.

    python3 -m unittest bi_finance.test_treasury_card

The stamp of treasury.json is checked by test_layout; this file checks what the card holds.
"""

import json
import os
import unittest
from collections import Counter

from bi_finance.test_layout import PACKAGE

WORKSPACE = os.path.join(PACKAGE, "bi_finance", "workspace", "treasury", "treasury.json")

# the card's links in the order Benchi uses them; MWST pro Quartal joins with erp-skbk
SWITZERLAND = [
    "VAT Declaration",
    "Payment Proposal",
    "Wise Settings",
    "PayPal Settings",
    "Bank Statement Upload",
    "Payroll Swiss Settings",
    "QST Tariff",
    "Payroll Hand-over",
]


def load():
    with open(WORKSPACE, encoding="utf-8") as f:
        return json.load(f)


class Card(unittest.TestCase):
    def setUp(self):
        self.doc = load()

    def card_links(self):
        links = self.doc["links"]
        start = next(i for i, link in enumerate(links) if link["type"] == "Card Break" and link["label"] == "Switzerland")
        out = []
        for link in links[start + 1:]:
            if link["type"] == "Card Break":
                break
            out.append(link)
        return out

    def test_the_card_has_its_links_in_order(self):
        self.assertEqual([link["label"] for link in self.card_links()], SWITZERLAND)

    def test_each_link_names_its_target(self):
        for link in self.card_links():
            self.assertTrue(link["link_to"], link["label"])
            self.assertIn(link["link_type"], ("DocType", "Report"), link["label"])

    def test_the_card_block_sits_on_the_page_before_the_number_cards(self):
        content = json.loads(self.doc["content"])
        cards = [i for i, block in enumerate(content) if block["type"] == "card"]
        self.assertEqual([content[i]["data"]["card_name"] for i in cards], ["Switzerland"])
        self.assertLess(cards[0], next(i for i, block in enumerate(content) if block["type"] == "number_card"))

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
        for link in self.card_links():
            if link["link_type"] == "Report":
                self.assertIn(link["link_to"], names, link["label"])
                self.assertEqual(link["is_query_report"], 1, link["label"])

    def test_bank_statement_upload_is_in_one_place_only(self):
        shortcuts = [s["label"] for s in self.doc["shortcuts"]]
        links = [link["label"] for link in self.doc["links"] if link["type"] == "Link"]
        self.assertEqual(shortcuts.count("Bank Statement Upload") + links.count("Bank Statement Upload"), 1)

    def test_no_label_is_both_a_shortcut_and_a_card_link(self):
        shortcuts = {s["label"] for s in self.doc["shortcuts"]}
        card = {link["label"] for link in self.card_links()}
        self.assertEqual(shortcuts & card, set())


if __name__ == "__main__":
    unittest.main()
