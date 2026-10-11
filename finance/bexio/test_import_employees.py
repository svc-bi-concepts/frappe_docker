"""Offline tests for import_employees.py against an in-memory ERPNext.

Invented employees only: names, numbers, dates and addresses are made up and
must never match a record of the real export (the privacy rule on the bead).
Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import os
import stat
import tempfile
import unittest

import import_employees as ie
from private_guard import setUpModule  # every test below runs against a temp private folder, never the real one


def invented_employee(**overrides):
    """One made-up employee record, every field filled."""
    emp = {
        "id": "test-emp-1",
        "personal_number": "T-001",
        "first_name": "Ada",
        "last_name": "Musterfrau",
        "gender": "female",
        "date_of_birth": "1980-01-02",
        "marital_status": "single",
        "nationality": "xx",
        "language": "de",
        "employment_level": 100,
        "hours_per_week": 40,
        "annual_vacation_days_total": 25,
        "ahv_number": "756.0000.0000.01",
        "iban": "CH00 0000 0000 0000 0000 0",
        "email": "ada.musterfrau@example.invalid",
        "phone_number": "+41 00 000 00 00",
        "stay_permit_category": "C",
        "address": {
            "street_name": "Testgasse", "house_number": "3", "street": "Testgasse 3",
            "complementary_line": None, "postbox": None, "zip_code": "8001",
            "city": "Teststadt", "locality": None, "canton": "ZH", "country": "CH",
            "municipality_id": 1,
        },
    }
    emp.update(overrides)
    return emp


def stubs(*month_lists):
    """Paystub index rows for one employee each: stubs([(2023, 11), (2024, 7)], ...) keyed by the ids above."""
    rows = []
    for i, months in enumerate(month_lists, start=1):
        for year, month in months:
            rows.append({"employee_id": "test-emp-{}".format(i), "year": year, "month": month,
                         "file": "test-emp-{}-{}-{:02d}.pdf".format(i, year, month), "bytes": 100})
    return rows


GENDERS = ["Male", "Female"]
EMPLOYMENT_TYPES = ["Full-time", "Part-time"]


class FakeErp:
    """Just enough of ERPNext: documents per doctype, list filters, insert, update."""

    def __init__(self, existing=None):
        self.docs = {}
        self.writes = 0
        self.counter = 0
        for doctype, docs in (existing or {}).items():
            for doc in docs:
                self.docs.setdefault(doctype, {})[doc["name"]] = copy.deepcopy(doc)

    def list(self, doctype, filters=None, fields=("name",)):
        rows = []
        for doc in self.docs.get(doctype, {}).values():
            if all(self._match(doc, f) for f in filters or []):
                rows.append({k: doc.get(k) for k in fields})
        return rows

    @staticmethod
    def _match(doc, flt):
        field, op, value = flt
        if op == "is":
            return bool(doc.get(field))
        return doc.get(field) == value

    def get(self, doctype, name):
        return copy.deepcopy(self.docs[doctype][name])

    def insert(self, doctype, doc):
        self.writes += 1
        doc = {k: v for k, v in copy.deepcopy(doc).items() if k != "doctype"}
        self.counter += 1
        doc["name"] = "HR-EMP-{:04d}".format(self.counter)
        self.docs.setdefault(doctype, {})[doc["name"]] = doc
        return {"name": doc["name"]}

    def update(self, doctype, name, values):
        self.writes += 1
        self.docs[doctype][name].update(copy.deepcopy(values))


def build(employees, paystubs, overrides=None, erp=None):
    """The plan against the fake site's Gender names."""
    return ie.build_plan(employees, paystubs, overrides or {},
                         [d["name"] for d in (erp or FakeErp()).docs.get("Gender", {}).values()] or GENDERS)


def import_all(erp, employees, paystubs, overrides=None, dry_run=False):
    plan = build(employees, paystubs, overrides, erp)
    importer = ie.EmployeeImporter(erp, dry_run=dry_run)
    importer.run(plan)
    return importer


class MappingTest(unittest.TestCase):
    def test_maps_every_field(self):
        erp = FakeErp()
        importer = import_all(erp, [invented_employee()], stubs([(2025, 1), (2026, 9)]))
        self.assertEqual(importer.counts.created, 1)
        doc = erp.docs["Employee"]["HR-EMP-0001"]
        self.assertEqual(doc["first_name"], "Ada")
        self.assertEqual(doc["last_name"], "Musterfrau")
        self.assertEqual(doc["gender"], "Female")
        self.assertEqual(doc["date_of_birth"], "1980-01-02")
        self.assertEqual(doc["marital_status"], "Single")
        self.assertEqual(doc["cell_number"], "+41 00 000 00 00")
        self.assertEqual(doc["personal_email"], "ada.musterfrau@example.invalid")
        self.assertEqual(doc["iban"], "CH00 0000 0000 0000 0000 0")
        self.assertEqual(doc["social_security_number"], "756.0000.0000.01")
        self.assertEqual(doc["company"], ie.COMPANY)
        self.assertEqual(doc["employment_type"], "Full-time")
        self.assertEqual(doc["swiss_employment_level"], 100)
        self.assertEqual(doc["swiss_hours_per_week"], 40)
        self.assertEqual(doc["swiss_stay_permit"], "C")
        self.assertEqual(doc["naming_series"], "HR-EMP-")
        self.assertEqual(doc["bexio_id"], "test-emp-1")

    def test_gender_matches_the_site_without_case(self):
        erp = FakeErp()
        import_all(erp, [invented_employee(gender="female")], stubs([(2025, 1)]))
        self.assertEqual(erp.docs["Employee"]["HR-EMP-0001"]["gender"], "Female")

    def test_marital_status_maps_and_unknown_is_left_empty(self):
        self.assertEqual(ie.marital_status("married"), "Married")
        self.assertEqual(ie.marital_status("divorced"), "Divorced")
        self.assertEqual(ie.marital_status("widowed"), "Widowed")
        self.assertEqual(ie.marital_status("partnership"), "")
        self.assertEqual(ie.marital_status(None), "")

    def test_employment_level_100_is_full_time_otherwise_part(self):
        erp = FakeErp()
        import_all(erp, [invented_employee(id="test-emp-1", employment_level=100),
                         invented_employee(id="test-emp-2", employment_level=60)],
                   stubs([(2025, 1)], [(2025, 1)]))
        self.assertEqual(erp.docs["Employee"]["HR-EMP-0001"]["employment_type"], "Full-time")
        self.assertEqual(erp.docs["Employee"]["HR-EMP-0002"]["employment_type"], "Part-time")

    def test_nationality_permit_and_language_go_into_the_bio(self):
        erp = FakeErp()
        import_all(erp, [invented_employee()], stubs([(2025, 1)]))
        bio = erp.docs["Employee"]["HR-EMP-0001"]["bio"]
        self.assertEqual(bio, "Nationality: xx\nLanguage: de")

    def test_missing_swiss_fields_and_swiss_nationals_stay_empty(self):
        erp = FakeErp()
        emp = invented_employee(employment_level=None, hours_per_week=None, stay_permit_category="Swiss")
        import_all(erp, [emp], stubs([(2025, 1)]))
        doc = erp.docs["Employee"]["HR-EMP-0001"]
        self.assertEqual(doc["swiss_employment_level"], "")
        self.assertEqual(doc["swiss_hours_per_week"], "")
        self.assertEqual(doc["swiss_stay_permit"], "")

    def test_bio_skips_what_the_record_does_not_carry(self):
        erp = FakeErp()
        import_all(erp, [invented_employee(stay_permit_category=None, language=None)],
                   stubs([(2025, 1)]))
        self.assertEqual(erp.docs["Employee"]["HR-EMP-0001"]["bio"], "Nationality: xx")

    def test_address_is_composed_from_the_dict(self):
        addr = {"street_name": "Testgasse", "house_number": "3", "street": "Testgasse 3",
                "complementary_line": None, "postbox": None, "zip_code": "8001",
                "city": "Teststadt", "locality": None, "canton": "ZH", "country": "CH",
                "municipality_id": 1}
        self.assertEqual(ie.composed_address(addr), "Testgasse 3\n8001 Teststadt")

    def test_address_falls_back_to_street_and_names_a_foreign_country(self):
        addr = {"street_name": None, "house_number": None, "street": "Langstrasse 9",
                "complementary_line": "c/o Test", "postbox": None, "zip_code": "8001",
                "city": "Teststadt", "locality": "Testdorf", "canton": "ZH", "country": "de",
                "municipality_id": 1}
        self.assertEqual(ie.composed_address(addr), "Langstrasse 9\nc/o Test\n8001 Testdorf\nDE")

    def test_address_empty_when_the_dict_is(self):
        self.assertEqual(ie.composed_address(None), "")
        self.assertEqual(ie.composed_address({}), "")


class DatesTest(unittest.TestCase):
    def test_joining_is_the_first_day_of_the_first_paystub_month(self):
        joined, _left = ie.dates_for([(2023, 11), (2024, 1)], (2026, 9))
        self.assertEqual(joined, "2023-11-01")

    def test_left_is_the_last_day_of_the_last_month_before_the_export(self):
        _joined, left = ie.dates_for([(2023, 11), (2024, 1), (2024, 2)], (2026, 9))
        self.assertEqual(left, "2024-02-29")

    def test_leap_february_gets_the_29th(self):
        self.assertEqual(ie.month_end((2024, 2)), "2024-02-29")
        self.assertEqual(ie.month_end((2025, 2)), "2025-02-28")

    def test_a_last_month_equal_to_the_export_stays_employed(self):
        _joined, left = ie.dates_for([(2025, 1), (2026, 9)], (2026, 9))
        self.assertEqual(left, "")

    def test_the_override_wins(self):
        joined, left = ie.dates_for([(2023, 11), (2026, 9)], (2026, 9),
                                    {"joined": "2020-03-02", "left": "2025-12-31"})
        self.assertEqual((joined, left), ("2020-03-02", "2025-12-31"))

    def test_an_override_left_empty_keeps_the_employee_active(self):
        joined, left = ie.dates_for([(2023, 11), (2024, 2)], (2026, 9), {"joined": "2020-03-02", "left": ""})
        self.assertEqual((joined, left), ("2020-03-02", ""))

    def test_no_paystubs_and_no_override_gives_no_dates(self):
        self.assertIsNone(ie.dates_for([], (2026, 9)))
        self.assertIsNone(ie.dates_for([], (2026, 9), {"joined": "", "left": ""}))

    def test_status_and_relieving_date_follow_the_dates(self):
        erp = FakeErp()
        import_all(erp, [invented_employee(id="test-emp-1"), invented_employee(id="test-emp-2")],
                   stubs([(2023, 11), (2024, 2)], [(2025, 8), (2026, 9)]))
        gone = erp.docs["Employee"]["HR-EMP-0001"]
        self.assertEqual(gone["status"], "Left")
        self.assertEqual(gone["relieving_date"], "2024-02-29")
        self.assertEqual(gone["date_of_joining"], "2023-11-01")
        here = erp.docs["Employee"]["HR-EMP-0002"]
        self.assertEqual(here["status"], "Active")
        self.assertNotIn("relieving_date", here)


class PlanTest(unittest.TestCase):
    def test_unknown_gender_is_reported_and_not_created(self):
        importer = import_all(FakeErp(), [invented_employee(gender="divers")], stubs([(2025, 1)]))
        self.assertEqual(importer.counts.created, 0)
        self.assertEqual(importer.counts.failed, 1)
        self.assertIn("unknown gender", importer.problems[0])
        self.assertNotIn("test-emp", importer.problems[0])

    def test_a_missing_gender_is_reported_too(self):
        importer = import_all(FakeErp(), [invented_employee(gender=None)], stubs([(2025, 1)]))
        self.assertEqual(importer.counts.created, 0)
        self.assertEqual(importer.counts.failed, 1)

    def test_without_paystubs_and_without_a_date_needs_joining_date(self):
        importer = import_all(FakeErp(), [invented_employee(id="test-emp-1")], [])
        self.assertEqual(importer.counts.created, 0)
        self.assertEqual(len(importer.needs_dates), 1)
        self.assertEqual(importer.needs_dates[0]["id"], "test-emp-1")


class UpsertTest(unittest.TestCase):
    def test_dry_run_writes_nothing_but_counts(self):
        erp = FakeErp()
        importer = import_all(erp, [invented_employee()], stubs([(2025, 1)]), dry_run=True)
        self.assertEqual(erp.writes, 0)
        self.assertEqual(importer.counts.created, 1)

    def test_rerun_creates_nothing_and_updates_nothing(self):
        erp = FakeErp()
        employees, paystubs = [invented_employee()], stubs([(2025, 1), (2026, 9)])
        import_all(erp, employees, paystubs)
        self.assertEqual(len(erp.docs["Employee"]), 1)
        importer = import_all(erp, employees, paystubs)
        self.assertEqual(importer.counts.created, 0)
        self.assertEqual(importer.counts.updated, 0)
        self.assertEqual(importer.counts.unchanged, 1)
        self.assertEqual(erp.writes, 1)

    def test_a_changed_field_updates_the_employee(self):
        erp = FakeErp()
        employees, paystubs = [invented_employee()], stubs([(2025, 1)])
        import_all(erp, employees, paystubs)
        employees[0]["phone_number"] = "+41 00 111 11 11"
        importer = import_all(erp, employees, paystubs)
        self.assertEqual(importer.counts.updated, 1)
        self.assertEqual(erp.docs["Employee"]["HR-EMP-0001"]["cell_number"], "+41 00 111 11 11")

    def test_existing_employees_count_as_existing(self):
        erp = FakeErp()
        employees, paystubs = [invented_employee()], stubs([(2025, 1)])
        import_all(erp, employees, paystubs)
        importer = import_all(erp, employees, paystubs)
        self.assertEqual(importer.counts.existing, 1)

    def test_override_dates_a_employee_without_paystubs(self):
        erp = FakeErp()
        overrides = {"test-emp-1": {"bexio_id": "test-emp-1", "joined": "2019-08-01", "left": "2026-03-31"}}
        import_all(erp, [invented_employee()], [], overrides)
        gone = erp.docs["Employee"]["HR-EMP-0001"]
        self.assertEqual(gone["date_of_joining"], "2019-08-01")
        self.assertEqual(gone["relieving_date"], "2026-03-31")
        self.assertEqual(gone["status"], "Left")


class FailingErp(FakeErp):
    def insert(self, doctype, doc):
        raise ie.ErpError(409, "insert Employee")


class WriteFailureTest(unittest.TestCase):
    def test_a_refused_write_is_counted_and_the_problem_carries_no_values(self):
        erp = FailingErp()
        importer = import_all(erp, [invented_employee(), invented_employee(id="test-emp-2")],
                              stubs([(2025, 1)], [(2025, 1)]))
        self.assertEqual(importer.counts.failed, 2)
        self.assertEqual(importer.counts.created, 0)
        self.assertEqual(len(importer.problems), 2)
        for problem in importer.problems:
            for value in ("Ada", "Musterfrau", "test-emp"):
                self.assertNotIn(value, problem)

    def test_the_count_line_says_what_was_done(self):
        counts = ie.Counts()
        counts.created = 2
        self.assertTrue(counts.line(True).startswith("to create: 2"))
        self.assertTrue(counts.line(False).startswith("created: 2"))


class CsvTest(unittest.TestCase):
    def test_a_missing_file_means_no_overrides(self):
        self.assertEqual(ie.read_dates_csv("/nonexistent/employee-dates.csv"), {})

    def test_rows_are_keyed_by_the_bexio_id(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            f.write("bexio_id,joined,left\ntest-emp-1,2019-08-01,\ntest-emp-2,2020-01-15,2025-12-31\n")
            path = f.name
        try:
            rows = ie.read_dates_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(rows["test-emp-1"]["joined"], "2019-08-01")
        self.assertEqual(rows["test-emp-2"]["left"], "2025-12-31")

    def test_the_header_may_spell_the_id_column_with_a_space(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            f.write("bexio id,joined,left\ntest-emp-1,2019-08-01,\n")
            path = f.name
        try:
            rows = ie.read_dates_csv(path)
        finally:
            os.unlink(path)
        self.assertIn("test-emp-1", rows)


class NeedsDatesTest(unittest.TestCase):
    def test_the_list_is_written_mode_600_with_a_count_header(self):
        emp = invented_employee(id="test-emp-9", first_name="Bea", last_name="Beispiel",
                                personal_number="T-009")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "needs-dates.txt")
            ie.write_needs_dates(path, [emp])
            mode = stat.S_IMODE(os.stat(path).st_mode)
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(lines[0], "# employees without a joining date: 1")
        self.assertIn("Bea Beispiel", lines[1])
        self.assertIn("test-emp-9", lines[1])

    def test_the_list_with_nobody_on_it_says_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "needs-dates.txt")
            ie.write_needs_dates(path, [])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read().splitlines(), ["# employees without a joining date: 0"])


class ReportTest(unittest.TestCase):
    def test_the_report_carries_counts_only(self):
        erp = FakeErp()
        employees = [invented_employee(id="test-emp-1"),
                     invented_employee(id="test-emp-2", first_name="Bea", last_name="Beispiel",
                                       employment_level=60)]
        plan = build(employees, stubs([(2023, 11), (2024, 2)], [(2025, 8), (2026, 9)]))
        importer = ie.EmployeeImporter(erp, dry_run=True)
        importer.run(plan)
        out = self.capture(lambda: ie.report(importer, plan, True))
        for name in ("Ada", "Musterfrau", "Bea", "Beispiel", "756", "test-emp"):
            self.assertNotIn(name, out)
        self.assertIn("records: 2", out)
        self.assertIn("status: Active 1, Left 1", out)
        self.assertIn("needs joining date: 0", out)
        self.assertIn("record 1 fields to write:", out)
        self.assertIn("swiss_employment_level", out)

    @staticmethod
    def capture(call):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            call()
        return buffer.getvalue()


class ExportMonthTest(unittest.TestCase):
    def test_the_export_last_month_comes_from_the_paystubs(self):
        paystubs = stubs([(2023, 11), (2026, 9)], [(2025, 8), (2026, 8)])
        _joined, left = ie.dates_for([(2025, 8), (2026, 8)], max(ie.month_key(s) for s in paystubs))
        # 2026-08 is before the export's 2026-09: Left on the month's end
        self.assertEqual(left, "2026-08-31")

    def test_an_empty_export_needs_dates_everywhere(self):
        plan = build([invented_employee()], [])
        self.assertEqual(plan[0][1], None)
        self.assertEqual(plan[0][2], "needs joining date")


if __name__ == "__main__":
    unittest.main()
