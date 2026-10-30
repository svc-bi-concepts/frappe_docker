"""Offline pairing and balance tests for bank_surplus.py. Invented values only."""

import contextlib
import io
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import bank_surplus as bs
from bi_finance import mt940
from private_guard import setUpModule  # noqa: F401


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
        rows = [
            row("BT-1", "2025-01-01", deposit="10", status="Unreconciled"),
            row("BT-2", "2025-01-01", deposit="10", status="Reconciled"),
            row("BT-3", "2025-01-01", deposit="10", transaction_id="MT-1"),
            row("BT-4", "2025-01-01", deposit="10", allocated_amount="1"),
        ]
        for count, expected_conflicts in (
            (1, ["BT-3", "BT-2"]),
            (2, ["BT-2"]),
            (3, []),
        ):
            with self.subTest(bank_lines=count):
                bank = [line("2025-01-01", deposit="10.00") for _ in range(count)]
                surplus, conflicts, _, _ = bs.pair(bank, rows)
                self.assertEqual([r["name"] for r in surplus], ["BT-1"])
                self.assertEqual([r["name"] for r in conflicts], expected_conflicts)

    def test_reports_protected_surplus_as_conflict(self):
        bank = [line("2025-01-01", deposit="10")]
        rows = [
            row("BT-1", "2025-01-01", deposit="10", allocated_amount="1"),
            row("BT-2", "2025-01-01", deposit="10", status="Reconciled"),
        ]
        surplus, conflicts, no_bank, no_row = bs.pair(bank, rows)
        self.assertEqual(surplus, [])
        self.assertEqual([r["name"] for r in conflicts], ["BT-2"])
        self.assertEqual((no_bank, no_row), (0, 0))

    def test_rows_without_a_bank_key_are_counted_but_never_cancelled(self):
        rows = [
            row("BT-1", "2025-01-01", deposit="10"),
            row("BT-2", "2025-01-01", withdrawal="10", status="Reconciled"),
        ]
        self.assertEqual(bs.pair([], rows), ([], [], 2, 0))

    def test_pairing_distinguishes_date_amount_and_direction_and_uses_name_order(self):
        bank = [line("2025-01-01", deposit="10")]
        rows = [
            row("BT-2", "2025-01-01", deposit="10"),
            row("BT-1", "2025-01-01", deposit="10"),
            row("BT-3", "2025-01-02", deposit="10"),
            row("BT-4", "2025-01-01", deposit="20"),
            row("BT-5", "2025-01-01", withdrawal="10"),
        ]
        surplus, conflicts, no_bank, no_row = bs.pair(bank, rows)
        self.assertEqual([r["name"] for r in surplus], ["BT-2"])
        self.assertEqual((conflicts, no_bank, no_row), ([], 3, 0))

    def test_protected_rows_cannot_be_candidates_when_bank_capacity_is_exhausted(self):
        for protection in (
            {"allocated_amount": "1"},
            {"payment_entries": [{"payment_entry": "PE-EXAMPLE"}]},
            {"transaction_id": "MT-EXAMPLE"},
            {"status": "Reconciled"},
        ):
            with self.subTest(protection=protection):
                rows = [
                    row(name, "2025-01-01", deposit="10", **protection)
                    for name in ("BT-1", "BT-2")
                ]
                surplus, conflicts, _, _ = bs.pair(
                    [line("2025-01-01", deposit="10")], rows
                )
                self.assertEqual(surplus, [])
                self.assertEqual([r["name"] for r in conflicts], ["BT-2"])

    def test_ignored_duplicate_is_a_conflict_not_a_cancel_candidate(self):
        rows = [
            row("BT-1", "2025-01-01", deposit="10", status="Ignored"),
            row("BT-2", "2025-01-01", deposit="10"),
        ]
        surplus, conflicts, _, _ = bs.pair(
            [line("2025-01-01", deposit="10")], rows
        )
        self.assertEqual([r["name"] for r in surplus], ["BT-2"])
        self.assertEqual([r["name"] for r in conflicts], [])

    def test_report_counts_kept_separately_and_retains_unmatched_rows_in_net(self):
        bank = [line("2025-01-01", deposit="10")]
        rows = [
            row("BT-1", "2025-01-01", deposit="10"),
            row("BT-2", "2025-01-01", deposit="10"),
            row("BT-3", "2025-01-02", withdrawal="10"),
        ]
        report = bs.report_text(bank, rows, *bs.pair(bank, rows))
        self.assertIn("kept: 1\nsurplus: 1\nconflicts: 0", report)
        self.assertIn("2025  10.00  10.00  0.00", report)

    def test_main_writes_private_outputs_with_gets_only_and_reads_allocations(self):
        with tempfile.TemporaryDirectory() as private:
            path = os.path.join(private, "example.mt940")
            with open(path, "w") as source:
                source.write(
                    ":20:EXAMPLE\n:25:CH3600000000000000000\n"
                    ":60F:C250101CHF1000,00\n"
                    ":61:2501020102C10,00NTRFNONREF//EXAMPLE\n"
                    ":62F:C250102CHF1010,00\n-\n"
                )
            erp = mock.Mock(spec=("list", "get"))
            erp.list.return_value = [
                row("BT-1", "2025-01-02", deposit="10"),
                row("BT-2", "2025-01-02", deposit="10"),
                row("BT-3", "2025-01-03", deposit="10"),
            ]
            erp.get.side_effect = lambda doctype, name: {
                "payment_entries": (
                    [{"payment_entry": "PE-EXAMPLE"}] if name == "BT-2" else []
                )
            }
            with mock.patch.object(bs.im, "PRIVATE", private), mock.patch.object(
                bs.im.Erp, "from_file", return_value=erp
            ), contextlib.redirect_stdout(io.StringIO()) as out:
                code = bs.main(
                    [
                        "--mt940",
                        path,
                        "--from",
                        "2025-01-01",
                        "--to",
                        "2025-01-31",
                        "--bank-account",
                        "Example bank",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("BT-", out.getvalue())
            self.assertNotIn("10.00", out.getvalue())
            erp.list.assert_called_once()
            self.assertIn(["docstatus", "=", 1], erp.list.call_args.args[1])
            self.assertIn(
                ["bank_account", "=", "Example bank"], erp.list.call_args.args[1]
            )
            stamp = bs.datetime.date.today().isoformat()
            with open(
                os.path.join(private, f"bexio-bank-surplus-{stamp}.txt")
            ) as names:
                self.assertEqual(names.read().strip(), "BT-1")

    def test_main_rejects_bad_balance_before_connecting_to_erp(self):
        with tempfile.TemporaryDirectory() as private:
            path = os.path.join(private, "example.mt940")
            with open(path, "w") as source:
                source.write(
                    ":20:EXAMPLE\n:25:CH3600000000000000000\n"
                    ":60F:C250101CHF1000,00\n:62F:C250102CHF1010,00\n-\n"
                )
            with mock.patch.object(bs.im, "PRIVATE", private), mock.patch.object(
                bs.im.Erp, "from_file"
            ) as connect, contextlib.redirect_stderr(io.StringIO()):
                code = bs.main(
                    [
                        "--mt940",
                        path,
                        "--from",
                        "2025-01-01",
                        "--to",
                        "2025-01-31",
                        "--bank-account",
                        "Example bank",
                    ]
                )
            self.assertEqual(code, 2)
            connect.assert_not_called()
            self.assertEqual(os.listdir(private), ["example.mt940"])

    def test_a_key_with_fewer_rows_than_bank_lines_counts_unmatched_bank_lines(self):
        bank = [line("2025-01-01", deposit="10"), line("2025-01-01", deposit="10")]
        surplus, conflicts, no_bank, no_row = bs.pair(
            bank, [row("BT-1", "2025-01-01", deposit="10")]
        )
        self.assertEqual((surplus, conflicts, no_bank, no_row), ([], [], 0, 1))

    def test_rejects_statement_with_closing_balance_that_does_not_tie(self):
        text = """:20:STATEMENT\n:25:CH3600000000000000000\n:60F:C250101CHF1000,00\n:61:2501020102C10,00NTRFNONREF//BANK1\n:62F:C250102CHF1011,00\n-\n"""
        statement = mt940.parse(text)
        with self.assertRaisesRegex(ValueError, "does not equal"):
            bs.check_statement(statement)

    def test_balanced_statement_passes(self):
        text = """:20:STATEMENT\n:25:CH3600000000000000000\n:60F:C250101CHF1000,00\n:61:2501020102C10,00NTRFNONREF//BANK1\n:62F:C250102CHF1010,00\n-\n"""
        bs.check_statement(mt940.parse(text))


if __name__ == "__main__":
    unittest.main()
