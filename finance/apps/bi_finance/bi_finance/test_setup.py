"""Offline tests for setup.py: after every migrate, the print formats whose HTML names
data.libracore.ch are switched off again (erpnextswiss's fixtures re-enable theirs on each
migrate), and the Property Setter fixture keeps 'BI Sales Invoice QR' the default print
format of Sales Invoice.

Invented Print Format rows, no database, no network. The modules import frappe, so run them
in the image (finance/docs/erpnext-setup.md), as with bi_finance.test_qrbill.
"""

import json
import os
import unittest
from unittest import mock

from bi_finance import setup

ROWS = ["QR Sales Invoice", "QR Sales Invoice Switzerland Rounding - Description Details"]


def run_with(rows):
    fake = mock.Mock()
    fake.get_all.return_value = rows
    with mock.patch.object(setup, "frappe", fake):
        returned = setup.disable_libracore_print_formats()
    return returned, fake


class DisableLibracorePrintFormats(unittest.TestCase):
    def test_disables_every_format_whose_html_names_libracore(self):
        returned, fake = run_with(list(ROWS))
        self.assertEqual(returned, ROWS)
        # the query itself carries the idempotence (only enabled ones) and the marker (the host)
        fake.get_all.assert_called_once_with(
            "Print Format",
            filters={"disabled": 0, "html": ["like", "%data.libracore.ch%"]},
            pluck="name",
        )
        for name in ROWS:
            fake.db.set_value.assert_any_call("Print Format", name, "disabled", 1, update_modified=False)
        self.assertEqual(fake.db.set_value.call_count, len(ROWS))

    def test_a_rerun_changes_nothing(self):
        returned, fake = run_with([])  # every match is disabled already
        self.assertEqual(returned, [])
        fake.db.set_value.assert_not_called()

    def test_after_migrate_runs_it(self):
        with mock.patch.object(setup, "disable_libracore_print_formats") as run:
            setup.after_migrate()
        run.assert_called_once_with()


class DefaultPrintFormatFixture(unittest.TestCase):
    # The Property Setter that keeps 'BI Sales Invoice QR' the default print format of Sales
    # Invoice, held in the app's fixtures so a migrate reapplies it.
    def fixture_rows(self):
        path = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)), "fixtures", "property_setter.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_fixture_holds_exactly_that_row(self):
        mine = [r for r in self.fixture_rows() if r.get("property") == "default_print_format"]
        self.assertEqual(len(mine), 1)
        row = mine[0]
        self.assertEqual(row["name"], "Sales Invoice-main-default_print_format")
        self.assertEqual(row["doctype"], "Property Setter")
        self.assertEqual(row["doc_type"], "Sales Invoice")
        self.assertEqual(row["doctype_or_field"], "DocType")
        self.assertEqual(row["property_type"], "Data")
        self.assertEqual(row["value"], "BI Sales Invoice QR")


if __name__ == "__main__":
    unittest.main()
