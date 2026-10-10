"""Tests of mwst_bexio with invented records: the shares of the receipts, the codes of an
invoice and a bill, the split of a manual entry, and the quarters and years of the export."""

import unittest
from decimal import Decimal

import mwst_bexio as mb

TAXES = {16: {"id": 16, "code": "UN77", "value": 7.7}, 28: {"id": 28, "code": "UN81", "value": 8.1},
         33: {"id": 33, "code": "BZM81", "value": 8.1}, 38: {"id": 38, "code": "VB81", "value": 8.1},
         3: {"id": 3, "code": "UEX", "value": 0}}
ACCOUNTS = [{"id": 11, "account_no": "1100", "account_type": 3},
            {"id": 12, "account_no": "6600", "account_type": 2},
            {"id": 13, "account_no": "3200", "account_type": 1},
            {"id": 14, "account_no": "1020", "account_type": 3}]


def invoice(invoice_id, total, total_net, taxs, positions, currency_id=1):
    return {"id": invoice_id, "total": total, "total_net": total_net, "taxs": taxs,
            "positions": positions, "currency_id": currency_id}


def position(tax_id, position_total):
    return {"tax_id": tax_id, "position_total": position_total}


def payrow(day, value, row_id=1, **kw):
    return dict({"date": day, "id": row_id, "value": value, "is_cash_discount": False,
                 "kb_credit_voucher_id": None}, **kw)


class QuartersTest(unittest.TestCase):
    def test_quarter_of_a_day(self):
        self.assertEqual(mb.quarter_of_day("2026-01-01"), "2026Q1")
        self.assertEqual(mb.quarter_of_day("2026-03-31"), "2026Q1")
        self.assertEqual(mb.quarter_of_day("2026-04-01"), "2026Q2")

    def test_quarters_from_the_first_to_the_last_day(self):
        self.assertEqual(mb.quarters_of(["2020-04-02", "2021-07-23"]),
                         ["2020Q2", "2020Q3", "2020Q4", "2021Q1", "2021Q2", "2021Q3"])
        self.assertEqual(mb.quarters_of([]), [])

    def test_years_without_a_receipt_move(self):
        journal = [
            {"date": "2020-05-01", "debit_account_id": 200, "credit_account_id": 127},
            {"date": "2022-05-01", "debit_account_id": 1, "credit_account_id": 2},
        ]
        accounts = [{"id": 200, "account_no": "2202"}, {"id": 127, "account_no": "2200"},
                    {"id": 1, "account_no": "1020"}, {"id": 2, "account_no": "3200"}]
        self.assertEqual(mb.years_without_receipt_moves(journal, accounts), ["2022"])


class InvoiceCodesTest(unittest.TestCase):
    def test_one_rate_uses_bexios_own_tax(self):
        codes, untaxed, total_net = mb.invoice_codes(
            invoice(1, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                    [position(16, "1000.00")]), TAXES)
        self.assertEqual(codes, {16: (Decimal("1000.00"), Decimal("77.00"))})
        self.assertEqual(untaxed, Decimal("0"))
        self.assertEqual(total_net, Decimal("1000.00"))

    def test_two_rates_share_bexios_tax_by_their_nets(self):
        codes, _untaxed, _net = mb.invoice_codes(
            invoice(2, "2158.00", "2000.00",
                    [{"percentage": "7.70", "value": "77.00"}, {"percentage": "8.10", "value": "81.00"}],
                    [position(16, "1000.00"), position(28, "1000.00")]), TAXES)
        self.assertEqual(codes[16], (Decimal("1000.00"), Decimal("77.00")))
        self.assertEqual(codes[28], (Decimal("1000.00"), Decimal("81.00")))

    def test_a_discount_spreads_over_the_codes(self):
        # the export carries no row of its own for the discount: the positions are 3040, the net is 260
        codes, _untaxed, _net = mb.invoice_codes(
            invoice(3, "280.02", "260.00", [{"percentage": "7.70", "value": "20.02"}],
                    [position(16, "1000.00"), position(16, "2040.00")]), TAXES)
        self.assertEqual(codes[16], (Decimal("260.00"), Decimal("20.02")))

    def test_a_rate_the_invoice_does_not_carry_taxes_nothing(self):
        # the position keeps its code, but the invoice was booked without the tax
        codes, _untaxed, _net = mb.invoice_codes(
            invoice(4, "1234.00", "1234.00", [], [position(16, "1234.00")]), TAXES)
        self.assertEqual(codes[16], (Decimal("1234.00"), Decimal("0")))

    def test_an_untaxed_code_keeps_its_net_out_of_the_taxed_rows(self):
        codes, untaxed, _net = mb.invoice_codes(
            invoice(5, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                    [position(16, "800.00"), position(3, "200.00")]), TAXES)
        self.assertEqual(codes[3], (Decimal("200.00"), Decimal("0")))
        self.assertEqual(untaxed, Decimal("0"))

    def test_a_foreign_invoice_takes_the_chf_bexio_booked(self):
        codes, untaxed, total_net = mb.invoice_codes(
            invoice(6, "1200.00", "1200.00", [], [position(3, "1200.00")], currency_id=3),
            TAXES, chf_booked=Decimal("1073.68"))
        self.assertEqual(total_net, Decimal("1073.68"))
        self.assertEqual(codes[3], (Decimal("1073.68"), Decimal("0")))
        self.assertEqual(untaxed, Decimal("0"))


class ReceiptSharesTest(unittest.TestCase):
    def test_a_full_payment_is_a_share_of_one(self):
        shares = list(mb.receipt_shares([payrow("2026-02-10", "1077.00")], Decimal("1077.00")))
        self.assertEqual(shares, [("2026-02-10", Decimal("1"), "payment")])

    def test_a_partial_payment_across_a_quarter_boundary(self):
        rows = [payrow("2026-03-31", "538.50"), payrow("2026-04-02", "538.50", row_id=2)]
        shares = list(mb.receipt_shares(rows, Decimal("1077.00")))
        self.assertEqual(shares[0][0], "2026-03-31")
        self.assertEqual(shares[1][0], "2026-04-02")
        self.assertEqual(shares[0][1] + shares[1][1], Decimal("1"))

    def test_a_payment_past_the_total_settles_nothing(self):
        rows = [payrow("2026-01-16", "3210.40"), payrow("2026-01-23", "3210.40", row_id=2)]
        shares = list(mb.receipt_shares(rows, Decimal("3210.40")))
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0][0], "2026-01-16")

    def test_a_credit_voucher_gives_back_what_was_paid(self):
        rows = [payrow("2026-01-10", "1000.00"),
                payrow("2026-02-01", "2468.25", row_id=2, kb_credit_voucher_id=4)]
        shares = list(mb.receipt_shares(rows, Decimal("2468.25")))
        self.assertEqual(shares[0], ("2026-01-10", Decimal("1000.00") / Decimal("2468.25"), "payment"))
        self.assertEqual(shares[1], ("2026-02-01", -Decimal("1000.00") / Decimal("2468.25"), "credit"))

    def test_a_cash_discount_settles_no_vat(self):
        rows = [payrow("2026-01-10", "50.00", is_cash_discount=True),
                payrow("2026-01-11", "1077.00", row_id=2)]
        shares = list(mb.receipt_shares(rows, Decimal("1077.00")))
        self.assertEqual(len(shares), 1)


class SalesTest(unittest.TestCase):
    def test_two_rates_on_one_invoice_count_in_their_rows(self):
        quarters = mb.Quarters()
        invoices = {7: invoice(7, "2158.00", "2000.00",
                               [{"percentage": "7.70", "value": "77.00"},
                                {"percentage": "8.10", "value": "81.00"}],
                               [position(16, "1000.00"), position(28, "1000.00")])}
        payments = [{"parent_id": 7, "rows": [payrow("2026-02-10", "2158.00")]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        totals = quarters.rows["2026Q1"]
        self.assertEqual(totals.rows["200"], [Decimal("2000.00"), Decimal("0")])
        self.assertEqual(totals.rows["302"], [Decimal("1000.00"), Decimal("77.00")])
        self.assertEqual(totals.rows["303"], [Decimal("1000.00"), Decimal("81.00")])

    def test_a_partial_payment_splits_over_the_quarters(self):
        quarters = mb.Quarters()
        invoices = {8: invoice(8, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                               [position(16, "1000.00")])}
        payments = [{"parent_id": 8, "rows": [payrow("2026-03-31", "538.50"),
                                              payrow("2026-04-02", "538.50", row_id=2)]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        self.assertEqual(quarters.rows["2026Q1"].rows["302"], [Decimal("500.00"), Decimal("38.50")])
        self.assertEqual(quarters.rows["2026Q2"].rows["302"], [Decimal("500.00"), Decimal("38.50")])

    def test_a_credit_voucher_counts_with_the_sign_over_what_was_paid(self):
        quarters = mb.Quarters()
        invoices = {9: invoice(9, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                               [position(16, "1000.00")])}
        payments = [{"parent_id": 9, "rows": [payrow("2026-01-10", "1000.00"),
                                              payrow("2026-06-09", "1077.00", row_id=2,
                                                     kb_credit_voucher_id=4)]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        share = Decimal("1000.00") / Decimal("1077.00")
        paid_base = (Decimal("1000.00") * share).quantize(Decimal("0.01"))
        paid_tax = (Decimal("77.00") * share).quantize(Decimal("0.01"))
        self.assertEqual(quarters.rows["2026Q1"].rows["302"], [paid_base, paid_tax])
        self.assertEqual(quarters.rows["2026Q2"].rows["302"], [-paid_base, -paid_tax])
        self.assertEqual(quarters.counts["credit-voucher offsets counted"], 1)

    def test_a_credit_voucher_over_an_unpaid_invoice_counts_nothing(self):
        # as in the export: the invoice was never paid, the credit note closes it in transit
        quarters = mb.Quarters()
        invoices = {10: invoice(10, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                                [position(16, "1000.00")])}
        payments = [{"parent_id": 10, "rows": [payrow("2026-06-09", "1077.00", kb_credit_voucher_id=4)]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        self.assertIsNone(quarters.rows.get("2026Q2"))
        self.assertEqual(quarters.counts["credit-voucher offsets counted"], 0)

    def test_an_advance_moves_no_vat(self):
        quarters = mb.Quarters()
        invoices = {11: invoice(11, "1077.00", "1000.00", [{"percentage": "7.70", "value": "77.00"}],
                                [position(16, "1000.00")])}
        payments = [{"parent_id": 11, "rows": [payrow("2026-01-16", "1077.00"),
                                               payrow("2026-01-23", "1077.00", row_id=2)]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        self.assertEqual(quarters.rows["2026Q1"].rows["302"], [Decimal("1000.00"), Decimal("77.00")])

    def test_the_tolerance_sums_the_row_bounds_of_the_contributions(self):
        quarters = mb.Quarters()
        invoices = {12: invoice(12, "1080.00", "1000.00", [{"percentage": "8.10", "value": "80.00"}],
                                [position(28, "1000.00")])}
        payments = [{"parent_id": 12, "rows": [payrow("2026-02-01", "540.00"),
                                               payrow("2026-02-20", "540.00", row_id=2)]}]
        mb.add_sales(quarters, invoices, payments, TAXES, {})
        self.assertEqual(quarters.tol[("2026Q1", "303")], mb.cent_bound(Decimal("8.1")))


class BillTest(unittest.TestCase):
    """The purchase side: one invented bill, paid in full on the payment day of its group."""

    ACCOUNTS = ACCOUNTS + [{"id": 40, "account_no": "2000"}, {"id": 41, "account_no": "1020"},
                           {"id": 42, "account_no": "1171"}, {"id": 43, "account_no": "1172"},
                           {"id": 44, "account_no": "2202"}, {"id": 45, "account_no": "2203"}]

    @staticmethod
    def bill():
        return {"id": "b-1", "document_no": "00001", "bill_date": "2026-01-05",
                "currency_code": "CHF", "exchange_rate": None, "item_net": False,
                "line_items": [{"amount": "1080.00", "tax_id": 38, "tax_calc": "80.00",
                                "booking_account_id": 12, "title": "Invented"}]}

    @staticmethod
    def journal():
        return [
            {"ref_class": "KbBill", "ref_uuid": "b-1", "ref_id": 1, "debit_account_id": 12,
             "credit_account_id": 40, "base_currency_amount": "1080.00",
             "date": "2026-01-05T00:00:00+01:00", "description": "Invented bill"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 40,
             "credit_account_id": 41, "base_currency_amount": "1080.00",
             "date": "2026-02-10T00:00:00+01:00", "description": "Zahlung ausgestellt"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 42,
             "credit_account_id": 43, "base_currency_amount": "80.00",
             "date": "2026-02-10T00:00:00+01:00", "description": "VAT transfer"},
        ]

    def test_a_bill_payment_counts_the_whole_bill_on_the_payment_day(self):
        quarters = mb.Quarters()
        mb.add_purchase(quarters, [self.bill()], self.journal(), self.ACCOUNTS, TAXES)
        # the line is gross: the net is 1000, the VAT is the 80 bexio gives
        self.assertEqual(quarters.rows["2026Q1"].rows["405"], [Decimal("1000.00"), Decimal("80.00")])
        self.assertEqual(quarters.counts["bill payments counted"], 1)
        self.assertEqual(quarters.counts["bills without a payment"], 0)

    def test_a_reverse_charge_bill_counts_its_tax_in_the_vorsteuer_and_on_the_day_bezug(self):
        quarters = mb.Quarters()
        bill = self.bill()
        bill["item_net"] = True
        bill["line_items"] = [{"amount": "3000.00", "tax_id": 33, "tax_calc": "243.00",
                               "booking_account_id": 12, "title": "Invented import"}]
        journal = [
            {"ref_class": "KbBill", "ref_uuid": "b-1", "ref_id": 1, "debit_account_id": 43,
             "credit_account_id": 40, "base_currency_amount": "3243.00",
             "date": "2026-01-05T00:00:00+01:00", "description": "Invented import"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 40,
             "credit_account_id": 41, "base_currency_amount": "3243.00",
             "date": "2026-02-10T00:00:00+01:00", "description": "Zahlung ausgestellt"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 42,
             "credit_account_id": 43, "base_currency_amount": "243.00",
             "date": "2026-02-10T00:00:00+01:00", "description": "VAT transfer"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 44,
             "credit_account_id": 45, "base_currency_amount": "243.00",
             "date": "2026-02-10T00:00:00+01:00", "description": "Bezugsteuer"},
        ]
        mb.add_purchase(quarters, [bill], journal, self.ACCOUNTS, TAXES)
        totals = quarters.rows["2026Q1"]
        self.assertEqual(totals.rows["400"], [Decimal("3000.00"), Decimal("243.00")])
        self.assertEqual(totals.rows["383"], [Decimal("3000.00"), Decimal("243.00")])

    def test_a_foreign_bill_converts_its_lines_at_bexios_rate(self):
        quarters = mb.Quarters()
        bill = self.bill()
        bill["currency_code"] = "USD"
        bill["exchange_rate"] = "0.814438"
        bill["line_items"] = [{"amount": "1110.00", "tax_id": 38, "tax_calc": "83.10",
                               "booking_account_id": 12, "title": "Invented abroad"}]
        journal = [
            {"ref_class": "KbBill", "ref_uuid": "b-1", "ref_id": 1, "debit_account_id": 12,
             "credit_account_id": 40, "base_currency_amount": "904.03",
             "date": "2026-01-05T00:00:00+01:00", "description": "Invented abroad"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 40,
             "credit_account_id": 41, "base_currency_amount": "904.03",
             "date": "2026-02-10T00:00:00+01:00", "description": "Zahlung ausgestellt"},
            {"ref_class": "KbClientAccountEntry", "ref_uuid": "p-1", "ref_id": 6, "debit_account_id": 42,
             "credit_account_id": 43, "base_currency_amount": "67.68",
             "date": "2026-02-10T00:00:00+01:00", "description": "VAT transfer"},
        ]
        mb.add_purchase(quarters, [bill], journal, self.ACCOUNTS, TAXES)
        # the net is the gross line less the given VAT, each at the rate
        self.assertEqual(quarters.rows["2026Q1"].rows["405"],
                         [Decimal("836.35"), Decimal("67.68")])


class ManualTest(unittest.TestCase):
    ROOTS = {12: 2, 13: 1, 14: 3, 40: 4, 41: 3, 44: 4, 45: 3}
    ACCOUNTS = [{"id": 12, "account_no": "6600", "account_type": 2},
                {"id": 13, "account_no": "3200", "account_type": 1},
                {"id": 14, "account_no": "1020", "account_type": 3},
                {"id": 40, "account_no": "2200", "account_type": 4},
                {"id": 41, "account_no": "1171", "account_type": 3},
                {"id": 44, "account_no": "2203", "account_type": 4},
                {"id": 45, "account_no": "1170", "account_type": 3}]

    def test_a_purchase_line_is_gross_and_its_rest_is_the_input_tax(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "1080.00", "debit_account_id": 12, "credit_account_id": 14,
             "tax_id": 38, "currency_factor": 1}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        self.assertEqual(quarters.rows["2026Q1"].journal["405"], [Decimal("999.07"), Decimal("80.93")])

    def test_a_sales_line_is_gross_and_its_rest_is_the_owed_tax(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "1080.00", "debit_account_id": 14, "credit_account_id": 13,
             "tax_id": 16, "currency_factor": 1}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        self.assertEqual(quarters.rows["2026Q1"].journal["302"], [Decimal("1002.79"), Decimal("77.21")])

    def test_a_reversal_counts_negative(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "1080.00", "debit_account_id": 13, "credit_account_id": 14,
             "tax_id": 16, "currency_factor": 1}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        self.assertEqual(quarters.rows["2026Q1"].journal["302"], [Decimal("-1002.79"), Decimal("-77.21")])

    def test_a_reverse_charge_line_is_net_and_counts_twice(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "3000.00", "debit_account_id": 12, "credit_account_id": 14,
             "tax_id": 33, "currency_factor": 1}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        journal = quarters.rows["2026Q1"].journal
        self.assertEqual(journal["400"], [Decimal("3000.00"), Decimal("243.00")])
        self.assertEqual(journal["383"], [Decimal("0.00"), Decimal("243.00")])

    def test_a_line_without_tax_or_at_zero_rate_counts_nothing(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "500.00", "debit_account_id": 12, "credit_account_id": 14, "tax_id": None,
             "currency_factor": 1},
            {"amount": "500.00", "debit_account_id": 12, "credit_account_id": 14,
             "tax_id": 3, "currency_factor": 1}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        self.assertEqual(quarters.rows, {})

    def test_a_foreign_line_converts_at_its_factor(self):
        quarters = mb.Quarters()
        entries = [{"date": "2026-03-05", "entries": [
            {"amount": "1080.00", "debit_account_id": 12, "credit_account_id": 14,
             "tax_id": 38, "currency_factor": "0.951702"}]}]
        mb.add_manual(quarters, entries, self.ACCOUNTS, TAXES)
        journal = quarters.rows["2026Q1"].journal["405"]
        self.assertEqual(journal[0] + journal[1], Decimal("1027.84"))


if __name__ == "__main__":
    unittest.main()
