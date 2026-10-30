"""Offline pairing and balance tests for bank_surplus.py. Invented values only."""

import unittest
from decimal import Decimal

import bank_surplus as bs
from bi_finance import mt940


def line(day, deposit="0", withdrawal="0"):
    return {
        "value_date": day,
        "deposit": Decimal(deposit),
        "withdrawal": Decimal(withdrawal),
    }


def row(name, day, deposit="0", withdrawal="0", **extra):
    return {
        "name": name,
        "date": day,
        "deposit": Decimal(deposit),
        "withdrawal": Decimal(withdrawal),
        "status": "Unreconciled",
        **extra,
    }


class BankSurplusTest(unittest.TestCase):
    def test_keeps_allocated_then_transaction_then_reconciled_then_lowest_name(self):
        bank = [line("2025-01-01", deposit="10.00") for _ in range(3)]
        rows = [
            row("BT-4", "2025-01-01", deposit="10", status="Unreconciled"),
            row("BT-3", "2025-01-01", deposit="10", status="Reconciled"),
            row("BT-2", "2025-01-01", deposit="10", transaction_id="MT-1"),
            row("BT-1", "2025-01-01", deposit="10", allocated_amount="1"),
        ]
        surplus, conflicts, _, _ = bs.pair(bank, rows)
        self.assertEqual([r["name"] for r in surplus], ["BT-4"])
        self.assertEqual([r["name"] for r in conflicts], [])

    def test_reports_protected_surplus_as_conflict(self):
        bank = []
        rows = [row("BT-1", "2025-01-01", deposit="10", status="Reconciled")]
        surplus, conflicts, no_bank, no_row = bs.pair(bank, rows)
        self.assertEqual(surplus, [])
        self.assertEqual([r["name"] for r in conflicts], ["BT-1"])
        self.assertEqual((no_bank, no_row), (1, 0))

    def test_a_key_with_fewer_rows_than_bank_lines_counts_unmatched_bank_lines(self):
        bank = [line("2025-01-01", deposit="10"), line("2025-01-01", deposit="10")]
        surplus, conflicts, no_bank, no_row = bs.pair(
            bank, [row("BT-1", "2025-01-01", deposit="10")]
        )
        self.assertEqual((surplus, conflicts, no_bank, no_row), ([], [], 0, 1))

    def test_rejects_statement_with_closing_balance_that_does_not_tie(self):
        text = """:20:STATEMENT\n:25:CH0000000000000000000\n:60F:C250101CHF1000,00\n:61:2501020102C10,00NTRFNONREF//BANK1\n:62F:C250102CHF1011,00\n-\n"""
        statement = mt940.parse(text)
        with self.assertRaisesRegex(ValueError, "does not equal"):
            bs.check_statement(statement)

    def test_balanced_statement_passes(self):
        text = """:20:STATEMENT\n:25:CH0000000000000000000\n:60F:C250101CHF1000,00\n:61:2501020102C10,00NTRFNONREF//BANK1\n:62F:C250102CHF1010,00\n-\n"""
        bs.check_statement(mt940.parse(text))


if __name__ == "__main__":
    unittest.main()
