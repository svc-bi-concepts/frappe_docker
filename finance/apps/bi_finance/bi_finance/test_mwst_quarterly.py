"""Offline tests of the 'MWST pro Quartal' report's pure half and of the drill-down's voucher tracking.

Invented vouchers and figures only (the repo is public). mwst_quarterly imports no frappe and neither does
mwst_payment, so this runs on the host as part of the depot's light gate.
"""

import datetime
import unittest
from decimal import Decimal

from bi_finance import mwst_payment as mp
from bi_finance import mwst_quarterly as mq

D = Decimal
ZERO = D("0")

# invented template names with bexio's ids, as the VAT Declaration's fill gives them
TEMPLATE_IDS = {
    "UN77 7.7% Normalsatz": "16",
    "UN81 8.1% Normalsatz": "28",
    "VM77 7.7% Normalsatz Material/DL": "22",
    "BZB81 8.1% Bezugsteuer Invest./Aufwand": "32",
}
YEAR = 2026
Q1, Q2 = f"{YEAR}Q1", f"{YEAR}Q2"


def row(name, tax, add_deduct="Add"):
    return {
        "description": name,
        "base_tax_amount": D(str(tax)),
        "tax_amount": D(str(tax)),
        "add_deduct_tax": add_deduct,
    }


def sale(posting, net, *rows):
    return {"posting_date": posting, "base_net_total": D(str(net)), "taxes": list(rows)}


def payment(name, posting, *refs):
    """A Payment Entry paying (doctype, name, allocated) references, as _payment_totals fetches them."""
    return {
        "name": name,
        "posting_date": posting,
        "references": [
            {
                "reference_doctype": dt,
                "reference_name": dn,
                "allocated_amount": D(str(al)),
            }
            for dt, dn, al in refs
        ],
    }


def invoice(grand, net, *rows):
    return {
        "grand_total": D(str(grand)),
        "base_net_total": D(str(net)),
        "taxes": list(rows),
    }


def period_totals(payments, invoices, journal=()):
    """The totals of one period, read the way the report's fetch does (mwst_payment.plan, journal_totals)."""
    sales, purchases, _unallocated = mp.payment_documents(payments, invoices)
    totals = mp.plan(sales, purchases, TEMPLATE_IDS)
    mp.journal_totals(list(journal), totals)
    return totals


def journal_line(voucher, day, account, amount, key=None):
    return {
        "voucher": voucher,
        "posting_date": day,
        "account": account,
        "amount": D(str(amount)),
        "key": key,
    }


class QuartersTest(unittest.TestCase):
    def test_a_year_is_its_four_quarters(self):
        self.assertEqual(mq.quarters_of(YEAR), [Q1, Q2, f"{YEAR}Q3", f"{YEAR}Q4"])

    def test_one_quarter_alone(self):
        self.assertEqual(mq.quarters_of(YEAR, "q3"), [f"{YEAR}Q3"])

    def test_the_filters_quarter_is_q1_to_q4_and_the_period_carries_the_year(self):
        # the Belege view takes its period from here: a filter value of 2026Q1 would be no quarter
        self.assertEqual(mq.quarters_of(YEAR, "Q1"), [Q1])
        self.assertEqual(mq.quarters_of(YEAR, Q1), [])

    def test_a_quarter_outside_the_form_is_refused(self):
        self.assertEqual(mq.quarters_of(YEAR, "Q5"), [])


class ZiffernRowsTest(unittest.TestCase):
    """The figures land in the right rows and columns: an invented Q1 of one 7.7 % sale paid in Q1,
    one 8.1 % sale paid in Q2, and bexio rows for Q1 only."""

    def setUp(self):
        invoice_77 = ("Sales Invoice", "INV-77", "1070.00")
        invoice_81 = ("Sales Invoice", "INV-81", "1081.00")
        self.totals_q1 = period_totals(
            [payment("ACC-PAY-Q1", datetime.date(YEAR, 2, 2), invoice_77)],
            {
                ("Sales Invoice", "INV-77"): invoice(
                    1070.00, 1000.00, row("UN77 7.7% Normalsatz", "70.00")
                )
            },
        )
        self.totals_q2 = period_totals(
            [payment("ACC-PAY-Q2", datetime.date(YEAR, 5, 2), invoice_81)],
            {
                ("Sales Invoice", "INV-81"): invoice(
                    1081.00, 1000.00, row("UN81 8.1% Normalsatz", "81.00")
                )
            },
        )
        self.figures = {
            Q1: mq.figures_of(self.totals_q1),
            Q2: mq.figures_of(self.totals_q2),
        }
        # bexio: Q1 only. Its own split derives the base from the tax the other way round, so a cent or
        # two stand between the bases (the tolerance mwst_ziffern_compare.py knows); the taxes are equal.
        self.bexio = {
            Q1: {
                "200": [D("1000.00"), ZERO],
                "302": [D("909.08"), D("70.00")],
                "399": [ZERO, D("70.00")],
            }
        }
        self.rows = {
            r["ziffer"]: r for r in mq.ziffern_rows([Q1, Q2], self.figures, self.bexio)
        }

    def test_a_rate_rows_figure_is_its_entgelt(self):
        # 302 is an Entgelt row: the base the payment basis derives from the quarter's tax, not the tax
        self.assertEqual(self.rows["302"][f"{Q1} erpnext"], D("909.09"))
        self.assertEqual(self.rows["302"][f"{Q2} erpnext"], ZERO)

    def test_the_bexio_column_joins_by_quarter_and_ziffer(self):
        self.assertEqual(self.rows["302"][f"{Q1} bexio"], D("909.08"))
        self.assertIsNone(self.rows["302"][f"{Q2} bexio"])

    def test_the_difference_is_erpnext_less_bexio(self):
        self.assertEqual(self.rows["302"][f"{Q1} differenz"], D("0.01"))
        self.assertIsNone(self.rows["302"][f"{Q2} differenz"])

    def test_a_quarter_without_bexio_rows_shows_blanks(self):
        for column in ("bexio", "differenz"):
            self.assertIsNone(self.rows["479"][f"{Q2} {column}"])

    def test_a_steuer_row_shows_the_tax(self):
        # 399 is a Steuer row: the quarter's owed tax; 400 a Vorsteuer; 500 the payable
        self.assertEqual(self.rows["399"][f"{Q1} erpnext"], D("70.00"))
        self.assertEqual(self.rows["399"][f"{Q2} erpnext"], D("81.00"))
        self.assertEqual(self.rows["500"][f"{Q1} erpnext"], D("70.00"))

    def test_a_row_the_module_does_not_compute_stays_blank(self):
        self.assertEqual(self.rows["220"][f"{Q1} erpnext"], ZERO)
        self.assertIsNone(self.rows["220"][f"{Q1} bexio"])

    def test_the_rows_are_the_form_rows_in_order(self):
        rows = mq.ziffern_rows([Q1], {}, {})
        self.assertEqual(
            [r["ziffer"] for r in rows], [z for z, _label, _kind in mq.ROWS]
        )

    def test_the_message_names_the_basis_and_counts_nothing(self):
        text = mq.message([Q1, Q2], with_bexio=True)
        self.assertIn("vereinnahmte Entgelte", text)
        self.assertIn(Q1, text)
        self.assertIn("220", text)


class DoctypeFloatsTest(unittest.TestCase):
    """The doctype hands bexio's figures as floats (a Currency column), the report's read as Decimals: the rows
    must not depend on which, and a Ziffer the module does not fill must subtract the zero it shows.
    """

    def test_floats_from_the_doctype_give_the_rows_the_decimals_do(self):
        figures = {Q1: {"302": [D("909.09"), D("70.00")], "399": [ZERO, D("70.00")]}}
        bexio_decimal = {
            Q1: {"302": [D("909.08"), D("70.00")], "399": [ZERO, D("70.00")]}
        }
        bexio_float = {Q1: {"302": [909.08, 70.0], "399": [0.0, 70.0]}}
        self.assertEqual(
            mq.ziffern_rows([Q1], figures, bexio_float),
            mq.ziffern_rows([Q1], figures, bexio_decimal),
        )

    def test_a_float_difference_is_cent_exact(self):
        rows = {
            r["ziffer"]: r
            for r in mq.ziffern_rows(
                [Q1],
                {Q1: {"302": [D("909.09"), D("70.00")]}},
                {Q1: {"302": [909.08, 70.0]}},
            )
        }
        self.assertEqual(rows["302"][f"{Q1} differenz"], D("0.01"))

    def test_bexio_without_a_module_figure_shows_zero_and_the_negative_difference(self):
        rows = {
            r["ziffer"]: r
            for r in mq.ziffern_rows([Q1], {}, {Q1: {"225": [1000.0, 0.0]}})
        }
        self.assertEqual(rows["225"][f"{Q1} erpnext"], ZERO)
        self.assertEqual(rows["225"][f"{Q1} bexio"], D("1000.00"))
        self.assertEqual(rows["225"][f"{Q1} differenz"], D("-1000.00"))

    def test_a_ziffer_without_a_bexio_row_keeps_both_columns_blank(self):
        rows = {
            r["ziffer"]: r
            for r in mq.ziffern_rows([Q1], {Q1: {"302": [D("909.09"), D("70.00")]}}, {})
        }
        self.assertEqual(rows["302"][f"{Q1} erpnext"], D("909.09"))
        self.assertIsNone(rows["302"][f"{Q1} bexio"])
        self.assertIsNone(rows["302"][f"{Q1} differenz"])


class BelegeTest(unittest.TestCase):
    """The drill-down: a Ziffer's vouchers, their shares summed per voucher, the journal entries among them."""

    def setUp(self):
        # one payment paying a 7.7 % and an 8.1 % invoice (one Ziffer each), a second payment adding to the 7.7 % one
        invoices = {
            ("Sales Invoice", "INV-A"): invoice(
                1070.00, 1000.00, row("UN77 7.7% Normalsatz", "70.00")
            ),
            ("Sales Invoice", "INV-B"): invoice(
                1081.00, 1000.00, row("UN81 8.1% Normalsatz", "81.00")
            ),
        }
        self.totals = period_totals(
            [
                payment(
                    "ACC-PAY-1",
                    datetime.date(YEAR, 2, 2),
                    ("Sales Invoice", "INV-A", "535.00"),
                    ("Sales Invoice", "INV-B", "540.50"),
                ),
                payment(
                    "ACC-PAY-2",
                    datetime.date(YEAR, 2, 20),
                    ("Sales Invoice", "INV-A", "535.00"),
                ),
            ],
            invoices,
            journal=[
                journal_line("ACC-JE-1", datetime.date(YEAR, 3, 1), "1170", D("35.00"))
            ],
        )

    def test_the_payment_entries_stand_under_their_ziffer_with_their_shares(self):
        belege = mq.belege_rows(self.totals, "302")
        self.assertEqual([b["name"] for b in belege], ["ACC-PAY-1", "ACC-PAY-2"])
        # a half share of a 70.00 tax is 35.00, whose base the module derives: 35 * 100 / 7.7
        self.assertEqual(belege[0]["base"], D("454.55"))
        self.assertEqual(belege[0]["tax"], D("35.00"))

    def test_one_ziffers_belege_are_not_another_ziffers(self):
        belege = mq.belege_rows(self.totals, "303")
        self.assertEqual([b["name"] for b in belege], ["ACC-PAY-1"])

    def test_the_journal_entries_among_the_belege(self):
        belege = mq.belege_rows(self.totals, "400")
        self.assertEqual(
            [(b["doctype"], b["name"]) for b in belege], [("Journal Entry", "ACC-JE-1")]
        )
        self.assertEqual(belege[0]["tax"], D("35.00"))

    def test_a_ziffer_nobody_paid_is_empty(self):
        self.assertEqual(mq.belege_rows(self.totals, "405"), [])

    def test_vouchers_of_a_plain_add_track_nothing_without_a_source(self):
        # the figures' callers that add without a document (mwst_bexio's Quarters) track no vouchers
        totals = mp.Totals()
        totals.add("302", D("1000.00"), D("70.00"))
        self.assertEqual(mq.belege_rows(totals, "302"), [])
        self.assertEqual(totals.tax_of("302"), D("70.00"))


class VoucherTrackingKeepsFiguresTest(unittest.TestCase):
    """The tracking rides along: a period's figures are the same with named payments as without."""

    def test_named_and_nameless_payments_give_the_same_rows(self):
        refs = [("Sales Invoice", "INV-A", "1070.00")]
        invoices = {
            ("Sales Invoice", "INV-A"): invoice(
                1070.00, 1000.00, row("UN77 7.7% Normalsatz", "70.00")
            )
        }
        named = period_totals(
            [payment("ACC-PAY-1", datetime.date(YEAR, 2, 2), *refs)], invoices
        )
        nameless = period_totals(
            [{"posting_date": datetime.date(YEAR, 2, 2), "references": [named_ref()]}],
            invoices,
        )
        for ziffer in ("200", "302", "399", "479"):
            self.assertEqual(named.tax_of(ziffer), nameless.tax_of(ziffer))
            self.assertEqual(named.base_of(ziffer), nameless.base_of(ziffer))


def named_ref():
    return {
        "reference_doctype": "Sales Invoice",
        "reference_name": "INV-A",
        "allocated_amount": D("1070.00"),
    }


if __name__ == "__main__":
    unittest.main()
