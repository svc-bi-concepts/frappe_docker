"""Offline checks of the Treasury files that migrate syncs: layout, names, and fields the doctypes have.

Reads the JSON files of the app and Frappe's own doctype definitions; no site, no network. Run it in
the image, as test_qrbill.py does (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/frappe-bench/apps/bi_finance:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/frappe-bench/apps/bi_finance && ../../env/bin/python -m unittest -v bi_finance.test_treasury'
"""

import json
import os
import re
import unittest
from unittest import mock

import frappe

from bi_finance import hooks
from bi_finance.bi_finance.report.cash_conversion_cycle import cash_conversion_cycle
from bi_finance.bi_finance.report.cash_position import cash_position
from bi_finance.patches import number_card_names

# The module folder: the Treasury files Frappe syncs (see test_layout.py).
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bi_finance")
FRAPPE_DOCTYPES = os.path.join(os.path.dirname(frappe.__file__), "desk", "doctype")
# Meta fields every exported document carries, next to the doctype's own fields.
META = {"doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}


def names(kind):
    return sorted(os.listdir(os.path.join(HERE, kind)))


def load(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as f:
        return json.load(f)


def doctype_fields(doctype):
    path = os.path.join(FRAPPE_DOCTYPES, frappe.scrub(doctype), frappe.scrub(doctype) + ".json")
    with open(path, encoding="utf-8") as f:
        return {field["fieldname"] for field in json.load(f)["fields"]} | META


def cards_of(report):
    docs = [load("number_card", name, name + ".json") for name in names("number_card")]
    return [doc for doc in docs if doc["report_name"] == report]


class Layout(unittest.TestCase):
    def test_each_file_sits_in_a_folder_named_after_its_document(self):
        # Frappe's sync reads <doctype folder>/<name>/<name>.json only.
        for folder, filename in [
            ("number_card", "total_chf"),
            ("number_card", "ccc_days"),
            ("number_card", "ccc_change_days"),
            ("dashboard_chart", "cash_position_history"),
            ("dashboard_chart", "cash_conversion_trend"),
            ("workspace", "treasury"),
        ]:
            self.assertTrue(os.path.isfile(os.path.join(HERE, folder, filename, filename + ".json")), filename)

    def test_the_hook_declares_the_two_doctypes_the_module_folders_hold(self):
        self.assertEqual(hooks.importable_doctypes, ["Number Card", "Dashboard Chart"])

    def test_every_number_card_file_is_named_after_its_folder(self):
        # The folder drops the punctuation of the name: "CCC (days)" is ccc_days. frappe.scrub would keep the
        # parentheses, and the sync takes the folder as it finds it (it loads <folder>/<folder>.json).
        for name in names("number_card"):
            doc = load("number_card", name, name + ".json")
            self.assertEqual(re.sub(r"[^a-z0-9]+", "_", doc["name"].lower()).strip("_"), name)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.ws = load("workspace", "treasury", "treasury.json")
        self.cards = {}
        for name in names("number_card"):
            doc = load("number_card", name, name + ".json")
            self.cards[doc["name"]] = doc
        self.charts = {}
        for name in names("dashboard_chart"):
            doc = load("dashboard_chart", name, name + ".json")
            self.charts[doc["name"]] = doc

    def test_content_shows_every_card_and_chart_once(self):
        blocks = json.loads(self.ws["content"])
        cards = [b["data"]["number_card_name"] for b in blocks if b["type"] == "number_card"]
        charts = [b["data"]["chart_name"] for b in blocks if b["type"] == "chart"]
        self.assertEqual(sorted(cards), sorted(self.cards))
        self.assertEqual(sorted(charts), sorted(self.charts))
        self.assertEqual(len({b["id"] for b in blocks}), len(blocks))

    def test_child_rows_match_the_content(self):
        blocks = json.loads(self.ws["content"])
        cards = [b["data"]["number_card_name"] for b in blocks if b["type"] == "number_card"]
        charts = [b["data"]["chart_name"] for b in blocks if b["type"] == "chart"]
        self.assertEqual([r["number_card_name"] for r in self.ws["number_cards"]], cards)
        self.assertEqual([r["chart_name"] for r in self.ws["charts"]], charts)

    def test_the_shortcuts_match_the_content_in_order(self):
        blocks = json.loads(self.ws["content"])
        shortcuts = [b["data"]["shortcut_name"] for b in blocks if b["type"] == "shortcut"]
        self.assertEqual(shortcuts, [r["label"] for r in self.ws["shortcuts"]])
        self.assertEqual(len(shortcuts), len(set(shortcuts)))

    def test_the_number_card_rows_are_labelled_with_their_names(self):
        # the page matches a number card block to its row by label (frappe's block.js), so the two must agree
        for row in self.ws["number_cards"]:
            self.assertEqual(row["label"], row["number_card_name"])

    def test_the_report_shortcuts_are_reports_of_this_app(self):
        cash = [r for r in self.ws["shortcuts"] if r["label"] in ("Cash Flow Forecast", "MWST pro Quartal")]
        self.assertEqual(sorted(r["label"] for r in cash), ["Cash Flow Forecast", "MWST pro Quartal"])
        for row in cash:
            self.assertEqual(row["type"], "Report")
            self.assertEqual(row["link_to"], row["label"])
            folder = row["link_to"].lower().replace(" ", "_")
            self.assertTrue(os.path.isfile(os.path.join(HERE, "report", folder, folder + ".json")), folder)
            self.assertTrue(set(row) <= doctype_fields("Workspace Shortcut"), set(row) - doctype_fields("Workspace Shortcut"))

    def test_every_card_and_chart_is_a_file_of_this_app(self):
        for row in self.ws["number_cards"]:
            self.assertIn(row["number_card_name"], self.cards)
        for row in self.ws["charts"]:
            self.assertIn(row["chart_name"], self.charts)

    def test_workspace_fields_exist_in_the_doctype(self):
        self.assertTrue(set(self.ws) <= doctype_fields("Workspace"), set(self.ws) - doctype_fields("Workspace"))


class Cards(unittest.TestCase):
    def test_cards_read_the_cash_position_report_chf_column(self):
        # The column labels are translated with a site; the field names are all this checks.
        with mock.patch.object(cash_position, "_", lambda text: text):
            columns = {c["fieldname"] for c in cash_position.columns()}
        for doc in cards_of("Cash Position"):
            self.assertEqual(doc["type"], "Report")
            self.assertEqual(doc["report_field"], "balance_chf")
            self.assertIn(doc["report_field"], columns)
            self.assertEqual(doc["function"], doc["report_function"])

    def test_cards_read_the_cash_conversion_report_and_its_latest_month(self):
        with mock.patch.object(cash_conversion_cycle, "_", lambda text: text):
            columns = {c["fieldname"] for c in cash_conversion_cycle.columns()}
        cards = cards_of("Cash Conversion Cycle")
        self.assertEqual(sorted(doc["report_field"] for doc in cards), ["ccc_12", "ccc_12_change"])
        for doc in cards:
            self.assertEqual(doc["type"], "Report")
            self.assertIn(doc["report_field"], columns)
            self.assertEqual(json.loads(doc["filters_json"]), {"latest_only": 1})

    def test_a_card_filters_on_its_chart_number_only(self):
        for doc in cards_of("Cash Position"):
            filters = json.loads(doc["filters_json"])
            if doc["name"] == "Total CHF":
                self.assertEqual(filters, {})
            else:
                self.assertEqual(list(filters), ["account_number"])

    def test_card_fields_exist_in_the_doctype(self):
        fields = doctype_fields("Number Card")
        for name in names("number_card"):
            doc = load("number_card", name, name + ".json")
            self.assertTrue(set(doc) <= fields, set(doc) - fields)

    def test_every_card_is_labelled_with_its_name_and_the_name_fits_column_three(self):
        """The page prints the workspace row's label and matches the card to its row by it, so label == name.

        20 characters is the limit the brief set for column three at 1440 px. It is not measured on the copy yet:
        the copy stack is down, so the live read-back after the switch is what measures it.
        """
        for name in names("number_card"):
            doc = load("number_card", name, name + ".json")
            self.assertEqual(doc["label"], doc["name"])
            self.assertLessEqual(len(doc["label"]), 20, doc["label"])

    def test_the_charts_fields_exist_in_the_doctype_and_they_use_their_report(self):
        fields = doctype_fields("Dashboard Chart")
        for name, report in [("cash_position_history", "Cash Position"), ("cash_conversion_trend", "Cash Conversion Cycle")]:
            chart = load("dashboard_chart", name, name + ".json")
            self.assertTrue(set(chart) <= fields, set(chart) - fields)
            self.assertEqual((chart["chart_type"], chart["report_name"]), ("Report", report))
        self.assertEqual(load("dashboard_chart", "cash_position_history", "cash_position_history.json")["use_report_chart"], 1)

    def test_the_charts_draw_their_months_as_a_time_series_and_the_position_chart_has_filters(self):
        # without timeseries the x axis reads "2.." for every month; without filters_json the widget stays on "Loading..."
        history = load("dashboard_chart", "cash_position_history", "cash_position_history.json")
        trend = load("dashboard_chart", "cash_conversion_trend", "cash_conversion_trend.json")
        self.assertEqual(history["timeseries"], 1)
        self.assertEqual(trend["timeseries"], 1)
        self.assertEqual(json.loads(history["filters_json"]), {})


class RetiredCards(unittest.TestCase):
    def test_the_patch_is_listed_after_the_model_sync(self):
        with open(os.path.join(HERE, "..", "patches.txt"), encoding="utf-8") as f:
            text = f.read()
        post = text.split("[post_model_sync]")[1]
        self.assertIn("bi_finance.patches.number_card_names", post)

    def test_the_patch_deletes_the_seven_old_names_and_no_current_card(self):
        # the old names are the cards before erp-hs4n; a current card must never be deleted by mistake
        current = {load("number_card", name, name + ".json")["name"] for name in names("number_card")}
        with mock.patch.object(frappe, "delete_doc") as delete_doc:
            number_card_names.execute()
        deleted = [c.args[1] for c in delete_doc.call_args_list]
        self.assertEqual(len(deleted), 7)
        self.assertEqual(set(deleted), set(number_card_names.OLD_NAMES))
        self.assertFalse(set(deleted) & current)
        for c in delete_doc.call_args_list:
            self.assertEqual(c.args[0], "Number Card")
            self.assertTrue(c.kwargs.get("ignore_missing"))


if __name__ == "__main__":
    unittest.main()
