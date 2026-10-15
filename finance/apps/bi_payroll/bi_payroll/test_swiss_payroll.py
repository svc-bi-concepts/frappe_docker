"""Offline tests of the Swiss payroll formulas, with invented employees and rates. No frappe needed:

    python3 -m unittest bi_payroll.test_swiss_payroll

from this app's directory. The formulas are read from fixtures/salary_component.json and evaluated with the
names HRMS gives them (slip fields, the assignment's insured salary, the copied rates, component abbreviations),
so a typo in a formula or a name missing from the fixtures fails here, not on the copy.
"""

import ast
import json
import os
import unittest
from datetime import date

from bi_payroll.swiss_rates import (
    ASSIGNMENT_FIELDS,
    RATE_FIELDS,
    STRUCTURE_DEDUCTIONS,
    STRUCTURE_EARNINGS,
    STRUCTURE_EMPLOYER_CONTRIBUTIONS,
    missing_components,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SETTINGS = os.path.join(
    HERE,
    "bi_payroll",
    "doctype",
    "payroll_swiss_settings",
    "payroll_swiss_settings.json",
)
SLIP_FUNCTIONS = {"min": min, "max": max, "int": int, "getdate": lambda d: d}
# swiss_qst_amount is set on the slip by withhold_qst (after gross_pay), and read by the QST component
SLIP_NAMES = {"gross_pay", "base", "end_date", "date_of_birth", "swiss_qst_amount"}


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


COMPONENTS = {c["salary_component_abbr"]: c for c in load("salary_component.json")}

# Invented rates: the placeholder values of the settings are only the defaults the site starts with, so the tests
# set their own, distinct per age band, to see which band a formula picks.
RATES = {
    "swiss_ahv_ee": 5.3,
    "swiss_ahv_ag": 5.3,
    "swiss_alv_ee": 1.1,
    "swiss_alv_ag": 1.1,
    "swiss_alv_ceiling": 148200,
    "swiss_bvg_threshold": 22680,
    "swiss_bvg_coordination": 26460,
    "swiss_bvg_ee_25": 1,
    "swiss_bvg_ee_35": 2,
    "swiss_bvg_ee_45": 3,
    "swiss_bvg_ee_55": 4,
    "swiss_bvg_ag_25": 1,
    "swiss_bvg_ag_35": 2,
    "swiss_bvg_ag_45": 3,
    "swiss_bvg_ag_55": 4,
    "swiss_uvg_max": 0,
    "swiss_uvg_ag": 0,
    "swiss_nbu_ee": 0,
    "swiss_ktg_ee": 0,
    "swiss_ktg_ag": 0,
    "swiss_fak_ag": 0,
}


def dob_for_age(end, age):
    return date(end.year - age, end.month, end.day)


def evaluate(abbr, ctx):
    return eval(
        COMPONENTS[abbr]["formula"], {"__builtins__": {}}, dict(SLIP_FUNCTIONS, **ctx)
    )


def run(
    abbr, end=date(2026, 10, 31), dob=date(1986, 3, 1), insured=0, rates=None, **slip
):
    # the slip's fields, the copied rates and the assignment, then the component's own formula
    ctx = dict(
        rates or RATES,
        swiss_bvg_insured_salary=insured,
        end_date=end,
        date_of_birth=dob,
        **slip,
    )
    # BVG Age comes before the BVG components in the structure, so it is in the context by its abbreviation
    if "BVGALTER" not in ctx:
        ctx["BVGALTER"] = run("BVGALTER", end=end, dob=dob) if abbr != "BVGALTER" else 0
    return evaluate(abbr, ctx)


def run_employer_pass(
    abbr, end=date(2026, 10, 31), dob=date(1986, 3, 1), insured=0, **slip
):
    # HRMS rebuilds the slip's data at the start of each pass: a statistical row (BVGALTER) is not in the employer pass
    ctx = dict(
        RATES, swiss_bvg_insured_salary=insured, end_date=end, date_of_birth=dob, **slip
    )
    return evaluate(abbr, ctx)


class Alv(unittest.TestCase):
    def test_below_the_ceiling_is_charged_in_full(self):
        self.assertAlmostEqual(run("ALV", gross_pay=10000), 110.0)

    def test_above_the_ceiling_only_the_monthly_ceiling_is_charged(self):
        # 148,200 a year is 12,350 a month: 20,000 pays on 12,350 only
        self.assertAlmostEqual(run("ALV", gross_pay=20000), 12350 * 1.1 / 100)

    def test_the_employer_share_has_the_same_ceiling(self):
        self.assertAlmostEqual(run("ALV_AG", gross_pay=20000), 12350 * 1.1 / 100)

    def test_ahv_has_no_ceiling(self):
        self.assertAlmostEqual(run("AHV", gross_pay=20000), 20000 * 5.3 / 100)


class Bvg(unittest.TestCase):
    def test_below_the_entry_threshold_nothing_is_charged(self):
        self.assertEqual(run("PK", insured=22679, gross_pay=1890), 0)

    def test_the_coordination_deduction_comes_off_the_insured_salary(self):
        # insured 60,000 a year, coordinated 33,540, age 40: band 35-44 at 2 percent of the monthly amount
        self.assertAlmostEqual(
            run("PK", insured=60000, gross_pay=5000), (60000 - 26460) / 12 * 2 / 100
        )

    def test_an_insured_salary_at_the_coordination_deduction_gives_nothing(self):
        # above the threshold, but no coordinated salary left
        self.assertEqual(run("PK", insured=26460, gross_pay=2205), 0)

    def test_each_age_band_takes_its_own_rate(self):
        end = date(2026, 10, 31)
        expected = {
            24: 0,
            25: 1,
            34: 1,
            35: 2,
            44: 2,
            45: 3,
            54: 3,
            55: 4,
            64: 4,
            65: 0,
        }
        for age, rate in expected.items():
            with self.subTest(age=age):
                got = run(
                    "PK",
                    end=end,
                    dob=dob_for_age(end, age),
                    insured=60000,
                    gross_pay=5000,
                )
                self.assertAlmostEqual(got, (60000 - 26460) / 12 * rate / 100)

    def test_the_employer_share_uses_the_employer_rates(self):
        rates = dict(RATES, swiss_bvg_ag_35=7)
        self.assertAlmostEqual(
            run("PK_AG", insured=60000, rates=rates), (60000 - 26460) / 12 * 7 / 100
        )
        # the employee share is not moved by the employer rate
        self.assertAlmostEqual(
            run("PK", insured=60000, rates=rates), (60000 - 26460) / 12 * 2 / 100
        )

    def test_the_employer_share_has_its_age_without_the_bvg_age_row(self):
        # erp-hzdn: the employer pass has no BVGALTER, so PK_AG works the age out of the dates itself. The rates
        # here are equal, so the employer share must equal the employee's own PK in its pass (BVGALTER present)
        end = date(2026, 10, 31)
        expected = {
            24: 0,
            25: 1,
            34: 1,
            35: 2,
            44: 2,
            45: 3,
            54: 3,
            55: 4,
            64: 4,
            65: 0,
        }
        for age, rate in expected.items():
            with self.subTest(age=age):
                dob = dob_for_age(end, age)
                got = run_employer_pass("PK_AG", end=end, dob=dob, insured=60000)
                self.assertAlmostEqual(
                    got, (60000 - RATES["swiss_bvg_coordination"]) / 12 * rate / 100
                )
                self.assertAlmostEqual(got, run("PK", end=end, dob=dob, insured=60000))

    def test_the_age_is_completed_years_at_the_period_end(self):
        # born 15 June 1990: 35 on 14 June 2026, 36 on 15 June 2026
        self.assertEqual(
            run("BVGALTER", end=date(2026, 6, 14), dob=date(1990, 6, 15)), 35
        )
        self.assertEqual(
            run("BVGALTER", end=date(2026, 6, 15), dob=date(1990, 6, 15)), 36
        )


class OpenInsurers(unittest.TestCase):
    def test_an_insurer_not_given_yet_charges_nothing(self):
        for abbr in ("NBUV", "UVG_AG", "KTG", "KTG_AG", "FAK_AG"):
            with self.subTest(abbr=abbr):
                self.assertEqual(run(abbr, gross_pay=8000), 0)

    def test_nbu_and_ktg_follow_their_rates_once_given(self):
        rates = dict(RATES, swiss_nbu_ee=1.2, swiss_ktg_ee=0.5, swiss_uvg_max=148200)
        self.assertAlmostEqual(
            run("NBUV", gross_pay=8000, rates=rates), 8000 * 1.2 / 100
        )
        self.assertAlmostEqual(
            run("KTG", gross_pay=8000, rates=rates), 8000 * 0.5 / 100
        )


class Quellensteuer(unittest.TestCase):
    def test_the_component_is_the_amount_set_on_the_slip(self):
        self.assertEqual(run("QST", swiss_qst_amount=57.75), 57.75)

    def test_no_code_or_no_gross_is_zero_and_the_component_is_left_out(self):
        self.assertEqual(run("QST", swiss_qst_amount=0), 0)


class Structure(unittest.TestCase):
    def test_every_structure_component_is_a_fixture(self):
        names = {c["name"] for c in load("salary_component.json")}
        structure = (
            STRUCTURE_EARNINGS + STRUCTURE_DEDUCTIONS + STRUCTURE_EMPLOYER_CONTRIBUTIONS
        )
        self.assertEqual(set(structure) - names, set())

    def test_each_component_is_in_one_table_of_the_structure(self):
        # a component in two tables would be paid twice (the slip's deductions and its employer contributions)
        structure = (
            STRUCTURE_EARNINGS + STRUCTURE_DEDUCTIONS + STRUCTURE_EMPLOYER_CONTRIBUTIONS
        )
        self.assertEqual(len(structure), len(set(structure)))

    def test_the_withholding_tax_is_a_deduction_after_the_gross(self):
        self.assertIn("Quellensteuer Employee", STRUCTURE_DEDUCTIONS)
        self.assertEqual(COMPONENTS["QST"]["type"], "Deduction")
        self.assertEqual(
            COMPONENTS["QST"]["salary_component"], "Quellensteuer Employee"
        )

    def test_the_withholding_amount_is_a_slip_field(self):
        slip = {
            f["fieldname"]
            for f in load("custom_field.json")
            if f["dt"] == "Salary Slip"
        }
        self.assertIn("swiss_qst_amount", slip)

    def test_a_complete_structure_lacks_nothing(self):
        self.assertEqual(
            missing_components(
                STRUCTURE_EARNINGS,
                STRUCTURE_DEDUCTIONS,
                STRUCTURE_EMPLOYER_CONTRIBUTIONS,
            ),
            [],
        )

    def test_a_structure_made_before_the_withholding_row_names_the_gap(self):
        # the structure erp-2s3c made: every component but the Quellensteuer one
        before = [c for c in STRUCTURE_DEDUCTIONS if c != "Quellensteuer Employee"]
        self.assertEqual(
            missing_components(
                STRUCTURE_EARNINGS, before, STRUCTURE_EMPLOYER_CONTRIBUTIONS
            ),
            ["Quellensteuer Employee"],
        )

    def test_a_structure_made_before_the_employer_contributions_names_them(self):
        # the live structure made before erp-nqyb's decision: the six shares were Deductions
        self.assertEqual(
            missing_components(STRUCTURE_EARNINGS, STRUCTURE_DEDUCTIONS, []),
            STRUCTURE_EMPLOYER_CONTRIBUTIONS,
        )

    def test_a_structure_without_an_earning_names_it_too(self):
        self.assertEqual(
            missing_components(
                [], STRUCTURE_DEDUCTIONS, STRUCTURE_EMPLOYER_CONTRIBUTIONS
            ),
            ["Basic Salary", "Commission"],
        )

    def test_a_structure_made_before_the_commission_row_names_the_gap(self):
        self.assertEqual(
            missing_components(
                ["Basic Salary"], STRUCTURE_DEDUCTIONS, STRUCTURE_EMPLOYER_CONTRIBUTIONS
            ),
            ["Commission"],
        )


class Commission(unittest.TestCase):
    # invented: 8,000 salary and 2,000 commission, so the gross is 10,000 in the month
    def test_the_commission_is_an_earning_in_the_gross(self):
        self.assertEqual(COMPONENTS["COMM"]["salary_component"], "Commission")
        self.assertEqual(COMPONENTS["COMM"]["type"], "Earning")
        self.assertEqual(COMPONENTS["COMM"]["statistical_component"], 0)
        self.assertEqual(COMPONENTS["COMM"]["do_not_include_in_total"], 0)
        self.assertEqual(COMPONENTS["COMM"]["amount_based_on_formula"], 0)
        self.assertEqual(COMPONENTS["COMM"]["depends_on_payment_days"], 0)

    def test_ahv_is_deducted_on_salary_plus_commission(self):
        self.assertAlmostEqual(run("AHV", gross_pay=10000), 10000 * 5.3 / 100)

    def test_alv_is_deducted_on_salary_plus_commission_up_to_the_ceiling(self):
        self.assertAlmostEqual(run("ALV", gross_pay=10000), 10000 * 1.1 / 100)
        # 12,000 salary and 1,000 commission: 13,000 is above the monthly ceiling of 12,350
        self.assertAlmostEqual(run("ALV", gross_pay=13000), 12350 * 1.1 / 100)

    def test_the_other_social_shares_are_on_salary_plus_commission(self):
        rates = dict(
            RATES,
            swiss_nbu_ee=1.2,
            swiss_ktg_ee=0.5,
            swiss_uvg_max=148200,
            swiss_uvg_ag=0.8,
        )
        self.assertAlmostEqual(
            run("NBUV", gross_pay=10000, rates=rates), 10000 * 1.2 / 100
        )
        self.assertAlmostEqual(
            run("KTG", gross_pay=10000, rates=rates), 10000 * 0.5 / 100
        )
        self.assertAlmostEqual(
            run("UVG_AG", gross_pay=10000, rates=rates), 10000 * 0.8 / 100
        )

    def test_bvg_stays_on_the_insured_salary_above_the_threshold(self):
        # the assignment's insured salary is fixed: the commission does not change the BVG base (hrms.md, question 3)
        with_commission = run("PK", insured=60000, gross_pay=10000)
        without = run("PK", insured=60000, gross_pay=8000)
        self.assertAlmostEqual(with_commission, (60000 - 26460) / 12 * 2 / 100)
        self.assertEqual(with_commission, without)

    def test_without_a_commission_the_deductions_are_unchanged(self):
        self.assertAlmostEqual(run("AHV", gross_pay=8000), 8000 * 5.3 / 100)
        self.assertAlmostEqual(run("ALV", gross_pay=8000), 8000 * 1.1 / 100)


class Fixtures(unittest.TestCase):
    def test_every_name_in_a_formula_is_known(self):
        known = (
            set(RATE_FIELDS)
            | set(ASSIGNMENT_FIELDS)
            | SLIP_NAMES
            | set(SLIP_FUNCTIONS)
            | set(COMPONENTS)
        )
        for abbr, component in COMPONENTS.items():
            if not component["formula"]:
                # a component paid by Additional Salary (Commission) has no formula to read names from
                continue
            names = {
                n.id
                for n in ast.walk(ast.parse(component["formula"], mode="eval"))
                if isinstance(n, ast.Name)
            }
            with self.subTest(abbr=abbr):
                self.assertEqual(names - known, set(), f"unknown names in {abbr}")

    def test_the_lohnausweis_codes_are_there(self):
        # erpnextswiss's Salary Certificate reads B, AHV, ALV, NBUV and PK (finance/docs/hrms.md)
        self.assertTrue({"B", "AHV", "ALV", "NBUV", "PK"} <= set(COMPONENTS))

    def test_no_thirteenth_salary_component(self):
        for component in COMPONENTS.values():
            self.assertNotIn("13", component["salary_component"])

    def test_each_rate_is_a_slip_copy_and_a_settings_field(self):
        slip = {
            f["fieldname"]
            for f in load("custom_field.json")
            if f["dt"] == "Salary Slip"
        }
        with open(SETTINGS, encoding="utf-8") as f:
            settings = {f["fieldname"] for f in json.load(f)["fields"]}
        self.assertTrue(set(RATE_FIELDS) <= slip)
        self.assertTrue(set(RATE_FIELDS) <= settings)

    def test_the_insured_salary_is_on_the_assignment(self):
        assignment = {
            f["fieldname"]
            for f in load("custom_field.json")
            if f["dt"] == "Salary Structure Assignment"
        }
        self.assertEqual(set(ASSIGNMENT_FIELDS), assignment)

    def test_the_public_2026_values_are_the_settings_defaults(self):
        with open(SETTINGS, encoding="utf-8") as f:
            defaults = {
                f["fieldname"]: f.get("default") for f in json.load(f)["fields"]
            }
        self.assertEqual(defaults["swiss_ahv_ee"], "5.3")
        self.assertEqual(defaults["swiss_alv_ceiling"], "148200")
        self.assertEqual(defaults["swiss_bvg_threshold"], "22680")
        self.assertEqual(defaults["swiss_bvg_coordination"], "26460")


class EmployerShares(unittest.TestCase):
    # erp-fs9c: the employer components were statistical, and HRMS keeps those off the slip, so the hand-over's
    # employer columns were 0. They are on the slip now, and out of gross and net (do_not_include_in_total).
    # erp-nqyb: an Employer Contribution, in the structure's employer_contributions table, not a deduction of the employee.
    def test_each_employer_share_is_on_the_slip(self):
        employers = [
            c
            for c in load("salary_component.json")
            if c["salary_component"].endswith("Employer")
        ]
        self.assertEqual(len(employers), 6)
        self.assertEqual(
            sorted(c["name"] for c in employers),
            sorted(STRUCTURE_EMPLOYER_CONTRIBUTIONS),
        )
        for c in employers:
            with self.subTest(component=c["salary_component"]):
                self.assertEqual(c["type"], "Employer Contribution")
                self.assertEqual(c["statistical_component"], 0)
                self.assertEqual(c["do_not_include_in_total"], 1)

    def test_no_employer_share_reads_a_statistical_row(self):
        # erp-hzdn: HRMS's employer pass has no statistical row in the context (BVGALTER is 0 there, so PK_AG dropped
        # out of every slip). An employer formula names only the slip's fields, rates and its own inline values.
        statistical = {
            c["salary_component_abbr"]
            for c in COMPONENTS.values()
            if c["statistical_component"]
        }
        self.assertTrue(
            statistical
        )  # not vacuous: the guard only means something while a statistical row exists
        for c in COMPONENTS.values():
            if c["type"] != "Employer Contribution" or not c["formula"]:
                continue
            names = {
                n.id
                for n in ast.walk(ast.parse(c["formula"], mode="eval"))
                if isinstance(n, ast.Name)
            }
            with self.subTest(abbr=c["salary_component_abbr"]):
                self.assertEqual(names & statistical, set())


if __name__ == "__main__":
    unittest.main()
