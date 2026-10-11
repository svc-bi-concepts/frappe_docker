"""Offline tests for parse_paystubs.py: an invented paystub text in bexio's layout, no PDF, no pdftotext, no private data.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import parse_paystubs as pp
from private_guard import setUpModule  # every test below runs against a temp private folder, never the real one

DE = open(os.path.join(os.path.dirname(__file__), "fixtures", "paystub-invented.txt"), encoding="utf-8").read()
EN = open(os.path.join(os.path.dirname(__file__), "fixtures", "paystub-invented-en.txt"), encoding="utf-8").read()

DE_5050 = "5050            BVG contribution                                                                         -440.00\n"
DE_5010 = "5010            AHV/IV/EO contribution                 5'500.00       5.300%                             -291.50\n"


def without(text, line):
    return text.replace(line, "", 1)


def drop_type(text, wage_type):
    """The text without the one line of that wage type."""
    return "".join(line + "\n" for line in text.splitlines() if not line.startswith(wage_type + " "))


class NumberTest(unittest.TestCase):
    def test_swiss_format(self):
        self.assertEqual(pp.to_number("6'100.55"), 6100.55)

    def test_negative_keeps_its_sign(self):
        self.assertEqual(pp.to_number("-323.35"), -323.35)

    def test_percent_is_stripped(self):
        self.assertEqual(pp.to_number("5.740%"), 5.74)
        self.assertEqual(pp.to_number("0.544%"), 0.544)
        self.assertEqual(pp.to_number("100.000%"), 100.0)


class ParseLineTest(unittest.TestCase):
    def test_amount_only(self):
        self.assertEqual(pp.parse_line("1001            Monthly salary 2                                                                           500.00"),
                         ("1001", "Monthly salary 2", None, None, 500.0))

    def test_base_rate_amount(self):
        parsed = pp.parse_line(DE_5010.strip())
        self.assertEqual(parsed, ("5010", "AHV/IV/EO contribution", 5500.0, 5.3, -291.5))

    def test_correction_with_base_and_amount_but_no_rate(self):
        parsed = pp.parse_line("5060            Correction Withholding tax                           5'500.00                             -315.70")
        self.assertEqual(parsed, ("5060", "Correction Withholding tax", 5500.0, None, -315.7))

    def test_rate_and_amount_without_base(self):
        parsed = pp.parse_line("1000            Monthly salary                         100.000%                                            5'000.00")
        self.assertEqual(parsed, ("1000", "Monthly salary", None, 100.0, 5000.0))

    def test_four_figures_take_the_first_as_base_and_the_last_as_amount(self):
        parsed = pp.parse_line("1005            Hourly wage   41.50   45.00   25.00   1'037.50")
        self.assertEqual(parsed, ("1005", "Hourly wage", 41.5, None, 1037.5))

    def test_no_wage_line(self):
        self.assertIsNone(pp.parse_line("Muster Maria"))
        self.assertIsNone(pp.parse_line("8000 Musterstadt"))
        self.assertIsNone(pp.parse_line("Der Betrag von CHF 3'350.00 wird überwiesen"))


class ParseTextTest(unittest.TestCase):
    def test_every_wage_type_is_parsed(self):
        rows = pp.parse_text(DE)
        self.assertEqual([row[0] for row in rows],
                         ["1000", "1001", "5000", "5010", "5020", "5040", "5050", "5060", "5070", "5900",
                          "6500", "6300.1", "6600"])
        self.assertEqual(rows[6], ("5050", "BVG contribution", None, None, -440.0))
        self.assertEqual(rows[7], ("5060", "Withholding tax", 5500.0, 5.74, -315.7))

    def test_the_address_postal_codes_are_not_wage_types(self):
        types = [row[0] for row in pp.parse_text(DE)]
        self.assertNotIn("8000", types)
        self.assertNotIn("8001", types)
        self.assertNotIn("4147", types)

    def test_the_english_layout_without_sections(self):
        rows = pp.parse_text(EN)
        self.assertEqual(rows[0], ("1218", "Commission", 10000.0, 12.0, 1200.0))
        self.assertEqual(len(rows), 7)

    def test_a_missing_line_leaves_the_others(self):
        rows = pp.parse_text(without(DE, DE_5050))
        self.assertEqual([row[0] for row in rows].count("5050"), 0)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[6], ("5060", "Withholding tax", 5500.0, 5.74, -315.7))

    def test_headers_alone_yield_nothing(self):
        self.assertEqual(pp.parse_text("Lohn und Zulagen\nAbzüge\nLohnart         Bezeichnung            Betrag CHF\n"), [])


class CheckTest(unittest.TestCase):
    ROWS = pp.parse_text(DE)

    def test_the_fixture_checks_out(self):
        self.assertEqual(pp.check_file(self.ROWS), "ok")

    def test_a_tampered_net_fails(self):
        rows = pp.parse_text(DE.replace("4'350.00", "4'350.10"))
        self.assertEqual(pp.check_file(rows), "fail")

    def test_without_gross_or_net_it_is_not_checkable(self):
        self.assertEqual(pp.check_file([row for row in self.ROWS if row[0] != "6500"]), "not_checkable")

    def test_five_rappen_are_rounding_not_a_difference(self):
        ok = [("5000", "", None, None, 100.0), ("5010", "", None, None, -50.05), ("6500", "", None, None, 50.00)]
        self.assertEqual(pp.check_file(ok), "ok")
        bad = [("5000", "", None, None, 100.0), ("5010", "", None, None, -50.06), ("6500", "", None, None, 50.00)]
        self.assertEqual(pp.check_file(bad), "fail")


class ParseRecordsTest(unittest.TestCase):
    RECORDS = [
        {"employee_id": "aaaa", "year": 2026, "month": 9, "file": "aaaa-2026-09.pdf", "bytes": 1},
        {"employee_id": "bbbb", "year": 2026, "month": 10, "file": "bbbb-2026-10.pdf", "bytes": 1},
    ]
    TEXTS = {"aaaa-2026-09.pdf": DE, "bbbb-2026-10.pdf": EN}

    def run_parse(self, texts=None, records=None):
        texts = texts or self.TEXTS
        with mock.patch.object(pp, "pdftotext_text", side_effect=lambda _, path: texts[os.path.basename(path)]):
            return pp.parse_records(records or self.RECORDS, "/tmp/paystubs")

    def test_rows_carry_the_employee_and_the_print_order(self):
        rows, per_file, counts, per_type = self.run_parse()
        self.assertEqual(counts["files"], 2)
        self.assertEqual(counts["parsed"], 2)
        self.assertEqual(counts["rows"], 20)
        self.assertEqual(counts["check_failed"], 0)
        self.assertEqual(rows[0]["employee_id"], "aaaa")
        self.assertEqual(rows[0]["month"], 9)
        self.assertEqual(rows[0]["wage_type"], "1000")
        self.assertEqual(per_type["5010"], 2)
        self.assertEqual(per_file["bbbb-2026-10.pdf"]["check"], "ok")
        # the English slip has no 1000/1001 monthly salary line
        self.assertEqual(counts["no_salary"], 1)

    def test_a_missing_5010_is_listed_on_the_file(self):
        texts = dict(self.TEXTS, **{"bbbb-2026-10.pdf": drop_type(EN, "5010")})
        _, per_file, _, _ = self.run_parse(texts)
        self.assertIn("no 5010 line", per_file["bbbb-2026-10.pdf"]["problems"])
        # the missing AHV line breaks the check too: the deductions no longer sum to gross minus net
        self.assertEqual(per_file["bbbb-2026-10.pdf"]["check"], "fail")

    def test_an_empty_pdftotext_output_counts_and_is_listed(self):
        texts = dict(self.TEXTS, **{"bbbb-2026-10.pdf": ""})
        _, per_file, counts, _ = self.run_parse(texts)
        self.assertEqual(counts["empty_text"], 1)
        self.assertEqual(counts["parsed"], 1)
        self.assertIn("scanned", per_file["bbbb-2026-10.pdf"]["problems"][0])

    def test_a_failed_check_counts(self):
        texts = dict(self.TEXTS, **{"aaaa-2026-09.pdf": DE.replace("4'350.00", "4'350.10")})
        _, _, counts, _ = self.run_parse(texts)
        self.assertEqual(counts["check_failed"], 1)


class PdftotextTest(unittest.TestCase):
    def test_layout_text_is_taken_from_stdout(self):
        done = mock.Mock(returncode=0, stdout="text", stderr="")
        with mock.patch.object(pp.subprocess, "run", return_value=done) as run:
            self.assertEqual(pp.pdftotext_text("/bin/pdftotext", "/p/a.pdf"), "text")
        run.assert_called_once_with(["/bin/pdftotext", "-layout", "/p/a.pdf", "-"],
                                    capture_output=True, text=True)

    def test_a_failure_raises_with_the_file_name_only(self):
        done = mock.Mock(returncode=1, stdout="", stderr="pdftotext: noise")
        with mock.patch.object(pp.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(RuntimeError, "a\\.pdf"):
                pp.pdftotext_text("/bin/pdftotext", "/p/a.pdf")


class WriteCsvTest(unittest.TestCase):
    def test_columns_figures_and_modes(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "payroll", "figures.csv")
        rows = [dict(zip(pp.CSV_COLUMNS, ["aaaa", 2026, 9, wage_type, label, base, rate, amount]))
                for wage_type, label, base, rate, amount in pp.parse_text(DE)]
        pp.write_csv(out, rows)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(out)).st_mode & 0o777, 0o700)
        with open(out, encoding="utf-8") as f:
            read = list(csv.DictReader(f))
        self.assertEqual(read[0]["wage_type"], "1000")
        self.assertEqual(read[0]["rate"], "100")
        self.assertEqual(read[0]["base"], "")
        self.assertEqual([r["wage_type"] for r in read].count("5050"), 1)
        qst = [r for r in read if r["wage_type"] == "5060"][0]
        self.assertEqual((qst["base"], qst["rate"], qst["amount"]), ("5500.00", "5.74", "-315.70"))
        nbu = [r for r in read if r["wage_type"] == "5040"][0]
        self.assertEqual(nbu["rate"], "0.544")


class WriteReportTest(unittest.TestCase):
    def test_problems_per_file_only_and_the_modes(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "payroll", "report.txt")
        per_file = {"aaaa-2026-09.pdf": {"types": ["1000"], "problems": ["no 5010 line"], "check": "ok"},
                    "bbbb-2026-10.pdf": {"types": ["1000"], "problems": [], "check": "ok"}}
        counts = {"files": 2, "parsed": 2, "empty_text": 0, "rows": 2, "check_failed": 0}
        pp.write_report(path, per_file, counts, {"1000": 2})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("files: 2", text)
        self.assertIn("lines per wage type: 1000 2", text)
        self.assertIn("no 5010 line", text)
        self.assertIn("aaaa-2026-09.pdf", text)
        self.assertNotIn("bbbb-2026-10.pdf\n", text)


class MainTest(unittest.TestCase):
    def run_main(self, extra, texts):
        tmp = tempfile.mkdtemp()
        json_path = os.path.join(tmp, "paystubs.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([{"employee_id": "aaaa", "year": 2026, "month": 9, "file": "a.pdf", "bytes": 1}], f)
        out, report = os.path.join(tmp, "o.csv"), os.path.join(tmp, "r.txt")
        argv = ["--paystubs-json", json_path, "--paystubs-dir", tmp, "--out", out, "--report", report] + extra
        with mock.patch.object(pp, "pdftotext_text", side_effect=lambda _, path: texts[os.path.basename(path)]):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = pp.main(argv)
        return code, buffer.getvalue(), out, report

    def test_counts_only_on_stdout_and_exit_zero(self):
        code, printed, out, report = self.run_main(["--check"], {"a.pdf": DE})
        self.assertEqual(code, 0)
        self.assertIn("files: 1", printed)
        self.assertIn("parsed: 1", printed)
        self.assertIn("checks failed: 0", printed)
        self.assertIn("wage-type rows: 13", printed)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(os.path.exists(report))

    def test_a_failed_check_exits_two_with_check(self):
        failing = DE.replace("4'350.00", "4'350.10")
        code, printed, _, _ = self.run_main(["--check"], {"a.pdf": failing})
        self.assertEqual(code, 2)
        self.assertIn("checks failed: 1", printed)

    def test_a_failed_check_without_check_still_exits_zero(self):
        failing = DE.replace("4'350.00", "4'350.10")
        code, printed, _, _ = self.run_main([], {"a.pdf": failing})
        self.assertEqual(code, 0)
        self.assertIn("checks failed: 1", printed)


if __name__ == "__main__":
    unittest.main()
