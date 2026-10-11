"""Tests of mwst_ziffern_compare with invented CSVs: the two readers, the reasons, and the exit."""

import os
import tempfile
import unittest
from decimal import Decimal

import mwst_ziffern_compare as zc
from private_guard import setUpModule  # every test below runs against a temp private folder, never the real one


def bexio_row(quarter, ziffer, base, tax, jbase="0.00", jtax="0.00", tol="0.00", stol="0.00"):
    return [quarter, ziffer, "label", base, tax, jbase, jtax, tol, stol]


def write_csv(path, rows, header=True):
    with open(path, "w", encoding="utf-8", newline="") as f:
        if header:
            f.write("quarter,ziffer,label,entgelt_chf,steuer_chf,journal_entgelt_chf,"
                    "journal_steuer_chf,entgelt_tol_chf,steuer_tol_chf\n")
        for row in rows:
            f.write(",".join('{}'.format(cell) for cell in row) + "\n")


def write_report(path, quarter, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("ziffer,label,entgelt_chf,steuer_chf\n")
        for row in rows:
            f.write(",".join(str(cell) for cell in row) + "\n")


class ReadBexioTest(unittest.TestCase):
    def test_the_rows_of_a_quarter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bexio.csv")
            write_csv(path, [bexio_row("2026Q1", "303", "100.00", "8.10", "10.00", "0.81", "0.08")])
            read = zc.read_bexio(path)
            self.assertEqual(read["2026Q1"]["303"],
                             (Decimal("100.00"), Decimal("8.10"), Decimal("10.00"),
                              Decimal("0.81"), Decimal("0.08"), Decimal("0.00")))


class ReadErpnextTest(unittest.TestCase):
    def test_the_total_and_its_journal_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mwst-2026Q1-payment.csv")
            write_report(path, "2026Q1", [
                ["405", "Vorsteuer", "90.00", "7.29"],
                ["405", zc.JOURNAL_LABEL, "10.00", "0.81"],
            ])
            read = zc.read_erpnext([path])
            self.assertEqual(read["2026Q1"]["405"],
                             (Decimal("90.00"), Decimal("7.29"), Decimal("10.00"), Decimal("0.81")))

    def test_a_quarter_without_a_journal_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mwst-2026Q2-payment.csv")
            write_report(path, "2026Q2", [["303", "Normalsatz", "5000.00", "405.00"]])
            read = zc.read_erpnext([path])
            self.assertEqual(read["2026Q2"]["303"], (Decimal("5000.00"), Decimal("405.00"), Decimal("0"), Decimal("0")))


class ReasonTest(unittest.TestCase):
    BEXIO = (Decimal("100.00"), Decimal("8.10"), Decimal("10.00"), Decimal("0.81"), Decimal("0.08"), Decimal("0.00"))

    def test_equal_rows_have_no_reason(self):
        self.assertEqual(zc.reason_of("303", self.BEXIO, (Decimal("100.00"), Decimal("8.10"), Decimal("10.00"), Decimal("0.81"))), "")

    def test_a_base_within_the_tolerance_is_rounding(self):
        self.assertEqual(zc.reason_of("303", self.BEXIO, (Decimal("100.05"), Decimal("8.10"), Decimal("10.00"), Decimal("0.81"))), "rounding")

    def test_a_difference_only_of_the_journal_parts_is_the_mirrors(self):
        # the report's journal rows keep the VAT mirrors; bexio's journal part is the manual entries
        self.assertEqual(zc.reason_of("303", self.BEXIO, (Decimal("90.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.81"))), "journal mirrors")

    def test_a_tax_difference_is_unexplained(self):
        self.assertEqual(zc.reason_of("303", self.BEXIO, (Decimal("100.00"), Decimal("8.11"), Decimal("10.00"), Decimal("0.81"))), "unexplained")

    def test_a_larger_base_difference_is_unexplained(self):
        self.assertEqual(zc.reason_of("303", self.BEXIO, (Decimal("200.00"), Decimal("8.10"), Decimal("10.00"), Decimal("0.81"))), "unexplained")


class DeductionReasonTest(unittest.TestCase):
    """The deduction rows 220, 221 and 225: bexio's declarations never carried a figure in them, the
    report fills them from the sales codes, so a bexio side with no row is the expected difference."""

    ABSENT = (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))

    def test_a_filled_deduction_row_over_an_absent_bexio_row_is_explained(self):
        erp = (Decimal("500.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("220", self.ABSENT, erp), "deduction rows")

    def test_a_bexio_figure_in_a_deduction_row_is_compared_as_any_row(self):
        bexio = (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.10"), Decimal("0.00"))
        erp = (Decimal("500.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("220", bexio, erp), "unexplained")

    def test_a_difference_on_303_is_not_the_deduction_reason(self):
        erp = (Decimal("500.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        self.assertNotEqual(zc.reason_of("303", self.ABSENT, erp), "deduction rows")


class TaxOnlyReasonTest(unittest.TestCase):
    """The Vorsteuer Ziffern 400 to 420, 479 and 500/510 are tax amounts on the form: their bases are
    information and not compared, whatever the journal parts' bases say."""

    BEXIO_405 = (Decimal("120.00"), Decimal("8.10"), Decimal("20.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.00"))

    def test_a_base_difference_alone_is_no_difference(self):
        # the tax agrees; the bases differ by the manual entries' nets, whose base the report's journal rows lack
        erp = (Decimal("0.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("405", self.BEXIO_405, erp), "")

    def test_a_tax_difference_within_the_tolerance_with_equal_journal_taxes_is_rounding(self):
        bexio = (Decimal("0.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.05"))
        erp = (Decimal("500.00"), Decimal("8.09"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("405", bexio, erp), "rounding")

    def test_a_tax_difference_is_unexplained_even_with_equal_journal_parts(self):
        bexio = (Decimal("0.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        erp = (Decimal("500.00"), Decimal("9.10"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("479", bexio, erp), "unexplained")

    def test_a_tax_difference_only_of_the_journal_parts_is_the_mirrors(self):
        bexio = (Decimal("0.00"), Decimal("8.10"), Decimal("0.00"), Decimal("8.10"), Decimal("0.00"), Decimal("0.00"))
        erp = (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(zc.reason_of("405", bexio, erp), "journal mirrors")


class CompareTest(unittest.TestCase):
    def test_a_quarter_without_an_erpnext_report_is_a_difference(self):
        bexio = {"2026Q1": {"303": (Decimal("100.00"), Decimal("8.10"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))}}
        rows = zc.compare(bexio, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], "no ERPNext report")
        self.assertEqual(len(zc.differences(rows)), 1)

    def test_an_erpnext_quarter_without_bexio_rows_shows_its_figures(self):
        erpnext = {"2026Q1": {"303": (Decimal("100.00"), Decimal("8.10"), Decimal("0"), Decimal("0"))}}
        rows = zc.compare({}, erpnext)
        self.assertEqual(rows[0][8], "unexplained")

    def test_equal_sides_have_no_differences(self):
        bexio = {"2026Q1": {"303": (Decimal("100.00"), Decimal("8.10"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))}}
        erpnext = {"2026Q1": {"303": (Decimal("100.00"), Decimal("8.10"), Decimal("0"), Decimal("0"))}}
        rows = zc.compare(bexio, erpnext)
        self.assertEqual(zc.differences(rows), [])
        self.assertIn("Ziffern with a difference: 0", zc.summary_lines(rows))

    def test_the_vorsteuer_total_479_is_classified_by_its_parts_journal(self):
        # the report's CSV holds no journal sub-row for a total, so the parts' journal tax is summed
        # first: a total whose tax differs only by the parts' journal part is the mirrors' residue
        bexio = {"2026Q1": {"479": (Decimal("0.00"), Decimal("53.50"), Decimal("0.00"), Decimal("53.50"), Decimal("0.00"), Decimal("0.00"))}}
        erpnext = {"2026Q1": {"405": (Decimal("100.00"), Decimal("13.00"), Decimal("0.00"), Decimal("13.00")),
                              "479": (Decimal("0.00"), Decimal("13.00"), Decimal("0.00"), Decimal("0.00"))}}
        rows = zc.compare(bexio, erpnext)
        row_479 = [row for row in rows if row[1] == "479"][0]
        self.assertEqual(row_479[8], "journal mirrors")


class MainTest(unittest.TestCase):
    def test_the_run_passes_on_equal_sides_and_exits_1_on_a_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            bexio_csv = os.path.join(tmp, "bexio.csv")
            write_csv(bexio_csv, [bexio_row("2026Q1", "303", "100.00", "8.10")])
            report_csv = os.path.join(tmp, "mwst-2026Q1-payment.csv")
            write_report(report_csv, "2026Q1", [["303", "Normalsatz", "100.00", "8.10"]])
            out = os.path.join(tmp, "compare.csv")
            self.assertEqual(zc.main(["--bexio", bexio_csv, "--reports", tmp, "--report", out]), 0)
            differing = os.path.join(tmp, "differing.csv")
            write_csv(differing, [bexio_row("2026Q1", "303", "100.00", "8.11")])
            self.assertEqual(zc.main(["--bexio", differing, "--reports", tmp, "--report", out]), 1)


if __name__ == "__main__":
    unittest.main()
