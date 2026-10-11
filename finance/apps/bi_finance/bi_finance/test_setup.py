"""Offline tests for setup.py: after every migrate, erpnextswiss's QR print formats are switched
off again (its fixtures re-enable them on each migrate): by name, by the libracore host in the
HTML, and by the QR helper's endpoint in the HTML. The Property Setter fixture keeps
'BI Sales Invoice QR' the default print format of Sales Invoice.

Invented Print Format rows, no database, no network. The modules import frappe, so run them
in the image (finance/docs/erpnext-setup.md), as with bi_finance.test_qrbill.
"""

import json
import os
import unittest
from unittest import mock

from bi_finance import setup

HERE = os.path.dirname(os.path.abspath(setup.__file__))
DETAILS = "QR Sales Invoice Switzerland Rounding - Description Details"


def fmt(name, html="<p>plain</p>", disabled=0):
    return {"name": name, "html": html, "disabled": disabled}


def get_all_over(formats):
    """frappe.get_all over the invented rows: the filters and or_filters the hook passes, applied in memory."""

    def hit(row, field, op, value):
        if op == "in":
            return row[field] in value
        assert op == "like" and value.startswith("%") and value.endswith("%"), (op, value)
        return value[1:-1] in row[field]

    def get_all(doctype, filters, or_filters, pluck):
        assert doctype == "Print Format" and pluck == "name"
        return [
            f["name"]
            for f in formats
            if all(f[k] == v for k, v in filters.items())
            and any(hit(f, field, op, value) for _, field, op, value in or_filters)
        ]

    return get_all


def run_with(formats):
    fake = mock.Mock()
    fake.get_all.side_effect = get_all_over(formats)
    with mock.patch.object(setup, "frappe", fake):
        returned = setup.disable_libracore_print_formats()
    return returned, fake


class DisableLibracorePrintFormats(unittest.TestCase):
    def test_name_match(self):
        # the two erpnextswiss QR formats are switched by name, whatever their HTML says
        for name in setup.QR_FORMATS:
            with self.subTest(name=name):
                returned, fake = run_with([fmt(name, html="<p>no host, no helper</p>")])
                self.assertEqual(returned, [name])
                fake.db.set_value.assert_called_once_with("Print Format", name, "disabled", 1, update_modified=False)

    def test_host_match(self):
        html = '<img src="https://data.libracore.ch/phpqrcode/api/iso20022.php?iban=x">'
        returned, fake = run_with([fmt("Some QR Format", html=html)])
        self.assertEqual(returned, ["Some QR Format"])
        fake.db.set_value.assert_called_once_with("Print Format", "Some QR Format", "disabled", 1, update_modified=False)

    def test_helper_match(self):
        # erpnextswiss's Description Details format calls the helper on another host
        html = '<img src="http://qr-code.example.test/api/iso20022.php?iban=x">'
        returned, fake = run_with([fmt("Helper Only", html=html)])
        self.assertEqual(returned, ["Helper Only"])
        fake.db.set_value.assert_called_once_with("Print Format", "Helper Only", "disabled", 1, update_modified=False)

    def test_our_own_format_is_untouched(self):
        # 'BI Sales Invoice QR' draws its QR locally: neither the host nor the helper is in its HTML
        with open(os.path.join(HERE, "bi_sales_invoice_qr.html"), encoding="utf-8") as f:
            ours = f.read()
        returned, fake = run_with([fmt("BI Sales Invoice QR", html=ours), fmt("Standard", html="<p>x</p>")])
        self.assertEqual(returned, [])
        fake.db.set_value.assert_not_called()

    def test_each_path_once_and_only_enabled_formats(self):
        formats = [
            fmt("QR Sales Invoice"),
            fmt(DETAILS),
            fmt("Some QR Format", html="https://data.libracore.ch/x"),
            fmt("Helper Only", html="https://h.example.test/api/iso20022.php"),
            fmt("Already Off", html="https://data.libracore.ch/x", disabled=1),
            fmt("Standard"),
        ]
        returned, fake = run_with(formats)
        self.assertEqual(returned, ["QR Sales Invoice", DETAILS, "Some QR Format", "Helper Only"])
        self.assertEqual(fake.db.set_value.call_count, 4)
        # the query itself carries the idempotence (only enabled ones) and the three markers
        filters = fake.get_all.call_args.kwargs["filters"]
        self.assertEqual(filters, {"disabled": 0})

    def test_a_rerun_changes_nothing(self):
        # every match is disabled already
        returned, fake = run_with([fmt("QR Sales Invoice", disabled=1), fmt(DETAILS, disabled=1)])
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
