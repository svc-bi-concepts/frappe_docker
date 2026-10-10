"""Offline tests for import_assignments.py against an in-memory ERPNext.

Invented employees, figures and settings only: names, numbers, dates and addresses are made up and must never match a
record of the real export (the privacy rule on the bead). Run with:
python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import csv
import datetime
import os
import stat
import tempfile
import unittest
from unittest import mock

import import_assignments as ia
from import_master import ErpError

COMPANY = ia.COMPANY
STRUCTURE = "Swiss Monthly BI Concepts"


def invented_employee(bexio_id="test-emp-1", dob="1980-01-02", canton="ZH"):
    """One made-up export record: the fields the importer reads."""
    return {"id": bexio_id, "first_name": "Ada", "last_name": "Musterfrau", "date_of_birth": dob,
            "address": {"canton": canton, "zip_code": "8001"}}


def figure(year, month, wage_type, amount=None, rate=None, base=None):
    return {"year": year, "month": month, "wage_type": wage_type, "base": base, "rate": rate, "amount": amount}


def invented_rows(salary=5000.0, bvg=None, qst=None, gross=None, ahv_rate=5.3):
    """The figures of two invented paystub months (January, February): the latest one is the base. A QST month
    prints its own gross (the 5060 bracket's income), the other lines are the salary's."""
    rows = []
    for month in (1, 2):
        month_gross = gross if (month == 2 and gross is not None) else salary
        rows += [figure(2026, month, "1000", amount=salary - 500, rate=None),
                 figure(2026, month, "1001", amount=salary),
                 figure(2026, month, "5010", amount=-salary * 0.053, rate=ahv_rate),
                 figure(2026, month, "5000", amount=month_gross),
                 figure(2026, month, "6500", amount=month_gross * 0.8)]
    if bvg is not None:
        rows.append(figure(2026, 2, "5050", amount=-bvg))
    if qst is not None:
        rows.append(figure(2026, 2, "5060", base=gross, rate=qst, amount=-gross * qst / 100))
    return rows


def settings(**overrides):
    """Invented settings: the BVG coordination and the band rates, the employee rates in percent."""
    values = {"swiss_bvg_coordination": 26460.0, "swiss_bvg_threshold": 22680.0,
              "swiss_bvg_ee_25": 7.0, "swiss_bvg_ee_35": 10.0, "swiss_bvg_ee_45": 15.0, "swiss_bvg_ee_55": 18.0,
              "swiss_ahv_ee": 5.3, "swiss_alv_ee": 1.1, "swiss_nbu_ee": None}
    values.update(overrides)
    return values


def tariff_row(code, rate, income_from=0.0, step=1000.0, valid_from="2026-01-01", canton="ZH"):
    return {"canton": canton, "tariff_code": code, "valid_from": valid_from, "income_from": income_from,
            "step": step, "rate": rate}


class FakeErp:
    """The REST calls the importer makes, on a dict. Writes are recorded for the assertions."""

    def __init__(self, employees=(), assignments=(), tariffs=(), settings_doc=None):
        self.employees = {row["name"]: dict(row) for row in employees}
        self.assignments = list(assignments)
        self.tariffs = list(tariffs)
        self.settings_doc = dict(settings_doc or settings())
        self.submitted = []
        self.updated = []
        self.filled = []

    def list(self, doctype, filters=None, fields=("name",)):
        if doctype == "Employee":
            return [dict(row) for row in self.employees.values()]
        if doctype == "Salary Structure":
            return [{"name": STRUCTURE}]
        if doctype == "Salary Structure Assignment":
            return [dict(row) for row in self.assignments if row.get("docstatus", 1) < 2]
        if doctype == "QST Tariff":
            return [dict(row) for row in self.tariffs]
        raise AssertionError(doctype)

    def insert(self, doctype, doc):
        doc = dict(doc, docstatus=0, doctype=doctype)
        self.assignments.append(doc)
        return doc

    def update(self, doctype, name, values):
        self.updated.append((name, dict(values)))
        self.employees[name].update(values)
        return self.employees[name]

    def call(self, method, **args):
        if method == "frappe.client.get":
            return dict(self.settings_doc)
        if method == "frappe.client.submit":
            self.submitted.append(args["doc"])
            args["doc"]["docstatus"] = 1
            return None
        if method == "frappe.client.set_value":
            self.filled.append(dict(args["fieldname"]))
            self.settings_doc.update(args["fieldname"])
            return None
        raise AssertionError(method)


def figures_of(rows):
    return {"test-emp-1": rows}


def erp_employee(name="HR-EMP-0001", status="Active", bexio_id="test-emp-1", qst_canton=None, qst_code=None):
    return {"name": name, "bexio_id": bexio_id, "status": status, "swiss_qst_canton": qst_canton,
            "swiss_qst_code": qst_code}


def run_plan(emp, rows, erp_row=None, settings_values=None, tariffs=(), existing=()):
    erp_row = erp_row or erp_employee()
    fixed = settings(**(settings_values or {}))
    plan = ia.build_plan(
        [emp], figures_of(rows), {erp_row["bexio_id"]: erp_row}, fixed, STRUCTURE,
        lambda canton: [t for t in tariffs if t["canton"] == canton], set(existing))
    return plan


class Base(unittest.TestCase):
    def assertProblem(self, plan, fragment):
        texts = [text for _pos, _name, text in plan.problems]
        self.assertTrue(any(fragment in text for text in texts), (fragment, texts))


class TestFigures(unittest.TestCase):
    def test_read_figures_numbers_and_months(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "figures.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["employee_id", "year", "month", "wage_type", "label", "base", "rate", "amount"])
                w.writerow(["test-emp-1", "2026", "2", "1001", "Monatslohn", "", "", "5000.00"])
                w.writerow(["test-emp-1", "2026", "2", "5010", "AHV", "", "5.3", "-265.00"])
            rows = ia.read_figures(path)
        self.assertEqual(rows["test-emp-1"][0]["amount"], 5000.0)
        self.assertIsNone(rows["test-emp-1"][0]["rate"])
        self.assertEqual(rows["test-emp-1"][1]["rate"], 5.3)
        self.assertEqual(ia.latest_line(rows["test-emp-1"], "5010")["rate"], 5.3)

    def test_latest_month_is_the_base(self):
        rows = [figure(2026, 1, "1001", amount=4000.0), figure(2026, 3, "1001", amount=5200.0),
                figure(2026, 2, "1001", amount=4100.0)]
        plan = run_plan(invented_employee(), rows)
        self.assertEqual(plan.entries[0]["assignment"]["base"], 5200.0)
        self.assertEqual(plan.entries[0]["assignment"]["from_date"], "2026-03-01")


class TestBvg(Base):
    def test_placeholder_rates_leave_the_insured_salary_empty(self):
        rows = invented_rows(bvg=600.0)
        plan = run_plan(invented_employee(), rows)
        self.assertNotIn("swiss_bvg_insured_salary", plan.entries[0]["assignment"])
        self.assertProblem(plan, "BVG insured salary unresolved: the band rate is a placeholder")

    def test_formula_inverse_resolves_a_round_figure(self):
        # insured 60000: (60000 - 26460) / 12 * 10 / 100 = 279.5; the band 35 to 44 rate of 10 percent
        with mock.patch.object(ia, "BVG_RATES_ARE_PLACEHOLDERS", False):
            plan = run_plan(invented_employee(dob="1985-06-01"), invented_rows(bvg=279.5))
        self.assertEqual(plan.entries[0]["assignment"]["swiss_bvg_insured_salary"], 60000)

    def test_a_figure_off_the_round_is_unresolved(self):
        with mock.patch.object(ia, "BVG_RATES_ARE_PLACEHOLDERS", False):
            plan = run_plan(invented_employee(dob="1985-06-01"), invented_rows(bvg=280.3))
        self.assertNotIn("swiss_bvg_insured_salary", plan.entries[0]["assignment"])
        self.assertProblem(plan, "not a round-ish figure")

    def test_age_bands_and_no_bvg_outside_25_to_64(self):
        on = datetime.date(2026, 2, 28)
        self.assertEqual(ia.bvg_band_field(ia.age_on(datetime.date(2001, 3, 1), on)), None)  # 24 years
        self.assertEqual(ia.bvg_band_field(ia.age_on(datetime.date(2001, 2, 28), on)), "swiss_bvg_ee_25")
        self.assertEqual(ia.bvg_band_field(ia.age_on(datetime.date(1991, 2, 28), on)), "swiss_bvg_ee_35")
        self.assertEqual(ia.bvg_band_field(ia.age_on(datetime.date(1971, 2, 28), on)), "swiss_bvg_ee_55")
        self.assertIsNone(ia.bvg_band_field(ia.age_on(datetime.date(1961, 2, 28), on)))  # 65

    def test_no_band_is_reported(self):
        plan = run_plan(invented_employee(dob="1950-01-01"), invented_rows(bvg=100.0))
        self.assertProblem(plan, "no BVG band at this age")


class TestQstCodes(unittest.TestCase):
    def test_unique_code_with_the_rate_in_the_bracket(self):
        tariffs = [tariff_row("AAA", 4.0), tariff_row("BBB", 5.5), tariff_row("CCC", 4.0, income_from=1000, step=1000)]
        self.assertEqual(ia.qst_codes(tariffs, "ZH", 500.0, 4.0, "2026-02-28"), ["AAA"])

    def test_ambiguous_codes_are_all_listed(self):
        tariffs = [tariff_row("AAA", 4.0), tariff_row("BBB", 4.0)]
        self.assertEqual(ia.qst_codes(tariffs, "ZH", 500.0, 4.0, "2026-02-28"), ["AAA", "BBB"])

    def test_no_code_for_the_rate(self):
        self.assertEqual(ia.qst_codes([tariff_row("AAA", 4.0)], "ZH", 500.0, 9.0, "2026-02-28"), [])

    def test_the_latest_valid_row_per_code_wins(self):
        tariffs = [tariff_row("AAA", 4.0, valid_from="2025-01-01"), tariff_row("AAA", 4.5, valid_from="2026-01-01")]
        self.assertEqual(ia.qst_codes(tariffs, "ZH", 500.0, 4.5, "2026-02-28"), ["AAA"])
        self.assertEqual(ia.qst_codes(tariffs, "ZH", 500.0, 4.0, "2026-02-28"), [])

    def test_rows_not_yet_valid_are_ignored(self):
        tariffs = [tariff_row("AAA", 4.0, valid_from="2027-01-01")]
        self.assertEqual(ia.qst_codes(tariffs, "ZH", 500.0, 4.0, "2026-02-28"), [])


class TestQuellensteuer(Base):
    def test_canton_and_code_resolved(self):
        tariffs = [tariff_row("AAA", 4.0, income_from=0, step=10000)]
        rows = invented_rows(qst=4.0, gross=5000.0)
        plan = run_plan(invented_employee(canton="ZH"), rows, tariffs=tariffs)
        self.assertEqual(plan.entries[0]["fields"], {"swiss_qst_canton": "ZH", "swiss_qst_code": "AAA"})
        self.assertEqual(plan.counts["codes resolved"], 1)

    def test_ambiguous_code_is_reported_with_the_count(self):
        tariffs = [tariff_row("AAA", 4.0, step=10000), tariff_row("BBB", 4.0, step=10000)]
        plan = run_plan(invented_employee(), invented_rows(qst=4.0, gross=5000.0), tariffs=tariffs)
        self.assertEqual(plan.entries[0]["fields"], {"swiss_qst_canton": "ZH"})
        self.assertProblem(plan, "2 candidate codes")

    def test_no_tariff_loaded_is_reported(self):
        plan = run_plan(invented_employee(), invented_rows(qst=4.0, gross=5000.0), tariffs=[])
        self.assertProblem(plan, "no tariff code")

    def test_no_5060_no_fields(self):
        plan = run_plan(invented_employee(), invented_rows())
        self.assertEqual(plan.entries[0]["fields"], {})
        self.assertEqual(plan.counts["no QST (no 5060)"], 1)

    def test_canton_missing_from_the_address_is_reported(self):
        plan = run_plan(invented_employee(canton=None), invented_rows(qst=4.0, gross=5000.0))
        self.assertNotIn("swiss_qst_canton", plan.entries[0]["fields"])
        self.assertProblem(plan, "the bexio address has none")


class TestAssignments(Base):
    def test_active_employee_gets_one_from_the_first_day(self):
        plan = run_plan(invented_employee(), invented_rows(salary=5000.0))
        self.assertEqual(plan.entries[0]["assignment"], {
            "employee": "HR-EMP-0001", "salary_structure": STRUCTURE, "from_date": "2026-02-01",
            "base": 5000.0, "company": COMPANY})

    def test_left_employee_gets_no_assignment(self):
        plan = run_plan(invented_employee(), invented_rows(), erp_row=erp_employee(status="Left"))
        self.assertIsNone(plan.entries[0]["assignment"])
        self.assertEqual(plan.counts["left, no assignment"], 1)

    def test_an_existing_assignment_is_not_made_again(self):
        plan = run_plan(invented_employee(), invented_rows(),
                        existing=[("HR-EMP-0001", "2026-02-01")])
        self.assertIsNone(plan.entries[0]["assignment"])
        self.assertEqual(plan.counts["assignments already made"], 1)

    def test_no_salary_line_in_the_latest_month_is_reported(self):
        rows = [figure(2026, 1, "1001", amount=5000.0), figure(2026, 2, "5010", amount=-265.0, rate=5.3)]
        plan = run_plan(invented_employee(), rows)
        self.assertIsNone(plan.entries[0]["assignment"])
        self.assertProblem(plan, "no monthly salary (1001)")

    def test_employee_without_an_erp_row_is_reported(self):
        plan = ia.build_plan([invented_employee()], figures_of(invented_rows()), {}, settings(), STRUCTURE,
                             lambda canton: [], set())
        self.assertProblem(plan, "no Employee for the bexio id")


class TestRates(Base):
    def test_setting_already_equal_is_not_filled(self):
        plan = ia.Plan()
        fills = ia.rate_report(plan, figures_of(invented_rows(ahv_rate=5.3)), settings(swiss_ahv_ee=5.3))
        self.assertNotIn("swiss_ahv_ee", fills)
        self.assertEqual(plan.counts["rates equal"], 1)

    def test_differing_setting_is_reported_not_overwritten(self):
        plan = ia.Plan()
        figs = figures_of(invented_rows(ahv_rate=5.3))
        fills = ia.rate_report(plan, figs, settings(swiss_ahv_ee=4.9))
        self.assertNotIn("swiss_ahv_ee", fills)
        self.assertProblem(plan, "rate swiss_ahv_ee: the setting differs")

    def test_empty_setting_is_filled(self):
        plan = ia.Plan()
        figs = figures_of(invented_rows(ahv_rate=5.3))
        fills = ia.rate_report(plan, figs, settings(swiss_ahv_ee=None))
        self.assertEqual(fills, {"swiss_ahv_ee": 5.3})

    def test_employer_rates_are_listed_for_the_insurer(self):
        plan = ia.Plan()
        ia.rate_report(plan, figures_of(invented_rows()), settings())
        self.assertEqual(plan.counts["rates needing insurer sheet"], len(ia.INSURER_FIELDS))


class TestWrites(Base):
    def erp(self, status="Active", assignments=()):
        return FakeErp(employees=[erp_employee(status=status)], assignments=list(assignments),
                       tariffs=[tariff_row("AAA", 4.0, step=10000)], settings_doc=settings(swiss_ahv_ee=None))

    def test_dry_run_writes_nothing(self):
        erp = self.erp()
        plan = run_plan(invented_employee(), invented_rows(qst=4.0, gross=5000.0),
                        tariffs=[tariff_row("AAA", 4.0, step=10000)])
        fills = ia.rate_report(plan, figures_of(invented_rows(qst=4.0, gross=5000.0)), settings(swiss_ahv_ee=None))
        writer = ia.Writer(erp, dry_run=True)
        writer.run(plan, fills, {"test-emp-1": erp_employee()})
        self.assertEqual(erp.assignments, [])
        self.assertEqual(erp.updated, [])
        self.assertEqual(erp.filled, [])

    def test_apply_creates_submits_and_the_rerun_creates_nothing(self):
        erp = self.erp()
        figs = invented_rows(qst=4.0, gross=5000.0)
        tariffs = [tariff_row("AAA", 4.0, step=10000)]
        plan = run_plan(invented_employee(), figs, tariffs=tariffs)
        fills = ia.rate_report(plan, figures_of(figs), settings(swiss_ahv_ee=None))
        writer = ia.Writer(erp, dry_run=False)
        writer.run(plan, fills, {"test-emp-1": erp_employee()})
        self.assertEqual(len(erp.submitted), 1)
        self.assertEqual(erp.submitted[0]["docstatus"], 1)
        self.assertEqual(erp.updated, [("HR-EMP-0001", {"swiss_qst_canton": "ZH", "swiss_qst_code": "AAA"})])
        self.assertEqual(erp.filled, [{"swiss_ahv_ee": 5.3}])
        self.assertEqual(writer.failures, [])

        # the rerun on the same figures: the assignment exists, the Employee fields and the settings are set
        erp.employees["HR-EMP-0001"].update({"swiss_qst_canton": "ZH", "swiss_qst_code": "AAA"})
        again = ia.build_plan([invented_employee()], figures_of(figs), {"test-emp-1": erp_employee(
            qst_canton="ZH", qst_code="AAA")}, settings(swiss_ahv_ee=5.3), STRUCTURE, lambda c: tariffs,
            ia.existing_assignments(erp))
        fills_again = ia.rate_report(again, figures_of(figs), settings(swiss_ahv_ee=5.3))
        writer2 = ia.Writer(erp, dry_run=False)
        writer2.run(again, fills_again, {"test-emp-1": erp_employee(qst_canton="ZH", qst_code="AAA")})
        self.assertEqual(len(erp.submitted), 1)
        self.assertEqual(len(erp.updated), 1)
        self.assertEqual(len(erp.filled), 1)
        self.assertEqual(writer2.counts["assignments made"], 0)

    def test_a_refused_write_is_counted_and_not_fatal(self):
        erp = self.erp()

        def refuse(*_args, **_kwargs):
            raise ErpError(417, "POST /resource/Salary Structure Assignment")

        erp.insert = refuse
        plan = run_plan(invented_employee(), invented_rows())
        writer = ia.Writer(erp, dry_run=False)
        writer.run(plan, {}, {"test-emp-1": erp_employee()})
        self.assertEqual(len(writer.failures), 1)
        self.assertIn("record 1: write refused", writer.failures[0])


class TestPrivacy(unittest.TestCase):
    def test_problems_file_is_mode_600(self):
        plan = ia.Plan()
        plan.add(1, "Ada Musterfrau", "no monthly salary (1001) in the latest paystub month")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "problems.txt")
            ia.write_problems(path, plan)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, encoding="utf-8") as f:
                self.assertIn("Ada Musterfrau", f.read())

    def test_stderr_names_the_position_only(self):
        self.assertEqual(ia.Plan.where(3), "record 3")
        self.assertEqual(ia.Plan.where(None), "settings")


if __name__ == "__main__":
    unittest.main()
