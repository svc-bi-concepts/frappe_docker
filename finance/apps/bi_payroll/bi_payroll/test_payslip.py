"""Offline tests of the payslip data (payslip.py) and the Lohnabrechnung template's render.
No frappe needed, an invented employee only (no real name, figure, rate or bank detail):

    python3 -m unittest bi_payroll.test_payslip

from this app's directory. The rates are invented placeholders; the bases mirror the component
formulas (fixtures/salary_component.json).
"""

import os
import unittest

from bi_payroll.payslip import money, pct, prepare

# Invented employee, born 1980-05-14: 46 years old at 2026-10-31, so the BVG group 45–54.
EMPLOYEE = {
    "employee_name": "Nina Muster",
    "social_security_number": "756.1234.5678.97",
    "employment_type": "Vollzeit",
    "date_of_birth": "1980-05-14",
    "bank_ac_no": "CH93 0076 2011 6238 5295 7",
    "swiss_qst_canton": "ZH",
    "swiss_qst_code": "C2",
}
ASSIGNMENT = {"swiss_bvg_insured_salary": 62460}

# Invented rates copied onto the slip (the settings' placeholders, not the real ones) and a slip
# whose rows the formulas give: AHV 424, ALV 88, BVG 300, NBU 40, KTG 35, QST 700 -> net 6413.
RATES = {
    "swiss_ahv_ee": 5.3, "swiss_ahv_ag": 5.3,
    "swiss_alv_ee": 1.1, "swiss_alv_ag": 1.1, "swiss_alv_ceiling": 148200,
    "swiss_bvg_threshold": 22680, "swiss_bvg_coordination": 26460,
    "swiss_bvg_ee_25": 8.0, "swiss_bvg_ee_35": 9.0, "swiss_bvg_ee_45": 10.0, "swiss_bvg_ee_55": 12.0,
    "swiss_bvg_ag_25": 16.0, "swiss_bvg_ag_35": 18.0, "swiss_bvg_ag_45": 20.0, "swiss_bvg_ag_55": 24.0,
    "swiss_uvg_max": 132300, "swiss_uvg_ag": 0.3,
    "swiss_nbu_ee": 0.5, "swiss_ktg_ee": 0.4375, "swiss_ktg_ag": 0.4375, "swiss_fak_ag": 0.75,
}


def line(component, amount, ytd=None):
    return {"salary_component": component, "amount": amount, "year_to_date": ytd}


def slip(**over):
    base = {
        "company": "Muster AG",
        "employee": "EMP-TEST-001",
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "gross_pay": 8000.0,
        "total_deduction": 1587.0,
        "net_pay": 6413.0,
        "rounded_total": 6415.0,
        "bank_account_no": "CH93 0076 2011 6238 5295 7",
        "earnings": [line("Basic Salary", 8000.0, 80000.0)],
        "deductions": [
            line("BVG Age", 46),
            line("AHV/IV/EO Employee", 424.0, 4240.0),
            line("ALV Employee", 88.0, 880.0),
            line("BVG Employee", 300.0, 3000.0),
            line("NBU Employee", 40.0, 400.0),
            line("KTG Employee", 35.0, 350.0),
            line("Quellensteuer Employee", 700.0, 7000.0),
        ],
        # HRMS keeps the employer shares in their own table (Employer Contribution components)
        "employer_contributions": [
            line("AHV/IV/EO Employer", 424.0, 4240.0),
            line("ALV Employer", 88.0, 880.0),
            line("BVG Employer", 600.0, 6000.0),
            line("UVG Employer", 24.0, 240.0),
            line("KTG Employer", 35.0, 350.0),
            line("FAK Employer", 60.0, 600.0),
        ],
        **RATES,
    }
    base.update(over)
    return base


def prepared(employee=None, assignment=None, **over):
    return prepare(
        slip(**over),
        employee=employee if employee is not None else dict(EMPLOYEE),
        assignment=assignment if assignment is not None else dict(ASSIGNMENT),
    )


def by_label(rows):
    return {row["label"]: row for row in rows}


class FormattingTest(unittest.TestCase):
    def test_swiss_thousands_separator_and_two_decimals(self):
        self.assertEqual(money(1234567.891), "1'234'567.89")
        self.assertEqual(money(-4.5), "-4.50")
        self.assertEqual(money(None), "")

    def test_rate_with_up_to_two_decimals_and_percent(self):
        self.assertEqual(pct(5.3), "5.3 %")
        self.assertEqual(pct(5), "5 %")
        self.assertEqual(pct(0.4375), "0.44 %")
        self.assertEqual(pct(None), "")


class HeaderTest(unittest.TestCase):
    def test_company_employee_and_period(self):
        d = prepared()
        self.assertEqual(d["company"], "Muster AG")
        self.assertEqual(d["employee_name"], "Nina Muster")
        self.assertEqual(d["employment"], "Vollzeit")
        self.assertEqual(d["period"], "01.10.2026 – 31.10.2026")

    def test_company_name_falls_back_to_the_slip_s_company_id(self):
        self.assertEqual(prepare(slip())["company"], "Muster AG")

    def test_ahv_number_shows_the_last_digits_only(self):
        self.assertEqual(prepared()["ahv_masked"], "•••••••••7897")

    def test_bank_account_shows_the_last_digits_only(self):
        self.assertEqual(prepared()["bank_last_digits"], "2957")

    def test_paid_amount_is_the_rounded_total(self):
        self.assertEqual(prepared()["paid_amount"], "6'415.00")

    def test_paid_amount_falls_back_to_the_net(self):
        d = prepared(rounded_total=None)
        self.assertEqual(d["paid_amount"], "6'413.00")

    def test_an_employee_without_fields_leaves_the_header_empty(self):
        d = prepare(slip())
        self.assertEqual(d["employee_name"], "")
        self.assertEqual(d["ahv_masked"], "")
        self.assertEqual(d["employment"], "")
        self.assertEqual(d["qst_canton"], "")


class DeductionRowsTest(unittest.TestCase):
    def test_rows_carry_label_base_rate_amount(self):
        rows = by_label(prepared()["deductions"])
        self.assertEqual(rows["AHV/IV/EO"], {"label": "AHV/IV/EO", "base": "8'000.00", "rate": "5.3 %", "amount": "424.00"})
        self.assertEqual(rows["ALV"]["base"], "8'000.00")
        self.assertEqual(rows["NBU"]["base"], "8'000.00")
        self.assertEqual(rows["KTG"]["rate"], "0.44 %")

    def test_bvg_base_is_the_monthly_coordinated_salary_at_the_age_group_s_rate(self):
        # (62'460 - 26'460) / 12 = 3'000 a month, age 46 -> the 45 group's rate.
        rows = by_label(prepared()["deductions"])
        self.assertEqual(rows["BVG"], {"label": "BVG", "base": "3'000.00", "rate": "10 %", "amount": "300.00"})
        self.assertEqual(by_label(prepared()["employer_shares"])["BVG"]["rate"], "20 %")

    def test_alv_base_stops_at_the_monthly_ceiling(self):
        rows = by_label(prepared(gross_pay=15000.0)["deductions"])
        self.assertEqual(rows["ALV"]["base"], "12'350.00")

    def test_nbu_base_stops_at_the_uvg_monthly_maximum(self):
        rows = by_label(prepared(gross_pay=15000.0)["deductions"])
        self.assertEqual(rows["NBU"]["base"], "11'025.00")

    def test_quellensteuer_names_canton_and_code_and_has_no_rate(self):
        rows = by_label(prepared()["deductions"])
        self.assertEqual(rows["Quellensteuer (ZH, C2)"], {"label": "Quellensteuer (ZH, C2)", "base": "8'000.00", "rate": "", "amount": "700.00"})

    def test_quellensteuer_without_canton_or_code_prints_plain(self):
        d = prepare(slip(), employee={k: v for k, v in EMPLOYEE.items() if not k.startswith("swiss_qst")}, assignment=dict(ASSIGNMENT))
        self.assertIn("Quellensteuer", by_label(d["deductions"]))

    def test_a_worker_under_25_has_no_bvg_rate(self):
        d = prepared(employee={**EMPLOYEE, "date_of_birth": "2005-05-14"})
        row = by_label(d["deductions"])["BVG"]
        self.assertEqual(row["rate"], "")
        self.assertEqual(row["base"], "0.00")

    def test_the_other_rates_print_without_a_birth_date_or_under_25(self):
        # only BVG depends on the age group: AHV, ALV, NBU, KTG and the employer rates apply at any age
        for employee in ({**EMPLOYEE, "date_of_birth": "2005-05-14"},
                         {k: v for k, v in EMPLOYEE.items() if k != "date_of_birth"}):
            d = prepared(employee=employee)
            rows = by_label(d["deductions"])
            self.assertEqual((rows["AHV/IV/EO"]["rate"], rows["ALV"]["rate"]), ("5.3 %", "1.1 %"))
            self.assertEqual(rows["NBU"]["rate"], "0.5 %")
            self.assertEqual(by_label(d["employer_shares"])["FAK"]["rate"], "0.75 %")

    def test_an_employee_without_birth_date_has_no_bvg_rate_or_base(self):
        d = prepared(employee={k: v for k, v in EMPLOYEE.items() if k != "date_of_birth"})
        row = by_label(d["deductions"])["BVG"]
        self.assertEqual((row["rate"], row["base"]), ("", "0.00"))

    def test_an_insured_salary_under_the_threshold_gives_no_base(self):
        d = prepared(assignment={"swiss_bvg_insured_salary": 20000})
        self.assertEqual(by_label(d["deductions"])["BVG"]["base"], "0.00")

    def test_an_unknown_deduction_prints_with_amount_only(self):
        d = prepared(deductions=slip()["deductions"] + [line("Gewinnbeteiligung Employee", 100.0)])
        row = by_label(d["deductions"])["Gewinnbeteiligung Employee"]
        self.assertEqual((row["base"], row["rate"], row["amount"]), ("", "", "100.00"))
        # and it stays out of the employer's block
        self.assertNotIn("Gewinnbeteiligung Employee", by_label(d["employer_shares"]))

    def test_the_bvg_age_helper_row_stays_off_the_print(self):
        d = prepared()
        self.assertNotIn("BVG Age", by_label(d["deductions"]))
        self.assertNotIn("BVG Age", by_label(d["employer_shares"]))


class EmployerBlockTest(unittest.TestCase):
    def test_employer_rows_come_separately_with_their_rates(self):
        rows = by_label(prepared()["employer_shares"])
        self.assertEqual(rows["AHV/IV/EO"]["rate"], "5.3 %")
        self.assertEqual(rows["UVG"]["rate"], "0.3 %")
        self.assertEqual(rows["FAK"]["rate"], "0.75 %")

    def test_employer_total_sums_the_printed_amounts(self):
        self.assertEqual(prepared()["employer_total"], "1'231.00")

    def test_the_employee_s_block_holds_no_employer_row(self):
        rows = by_label(prepared()["deductions"])
        self.assertNotIn("AHV/IV/EO Employer", rows)
        self.assertNotIn("FAK", rows)
        self.assertNotIn("UVG", rows)


class EarningsAndTotalsTest(unittest.TestCase):
    def test_earnings_print_with_german_labels(self):
        self.assertEqual(prepared()["earnings"], [{"label": "Grundlohn", "amount": "8'000.00"}])

    def test_an_unknown_earning_prints_with_its_own_name(self):
        d = prepared(earnings=[line("Basic Salary", 8000.0), line("Gewinnbeteiligung", 500.0)])
        self.assertIn({"label": "Gewinnbeteiligung", "amount": "500.00"}, d["earnings"])

    def test_the_commission_earning_prints_german(self):
        # the Commission component of erp-ca04 sits in the gross
        d = prepared(earnings=[line("Basic Salary", 8000.0), line("Commission", 500.0)], gross_pay=8500.0)
        self.assertIn({"label": "Provision", "amount": "500.00"}, d["earnings"])

    def test_gross_total_deduction_and_net(self):
        d = prepared()
        self.assertEqual(d["gross"], "8'000.00")
        self.assertEqual(d["total_deduction"], "1'587.00")
        self.assertEqual(d["net"], "6'413.00")

    def test_year_to_date_gross_sums_the_earnings_rows(self):
        self.assertEqual(prepared()["year_to_date_gross"], "80'000.00")

    def test_year_to_date_net_takes_the_employee_s_rows_only(self):
        # 80'000 gross - (4'240 AHV + 880 ALV + 3'000 BVG + 400 NBU + 350 KTG + 7'000 QST) =
        # 64'130, ten months of the 6'413 net.
        self.assertEqual(prepared()["year_to_date_net"], "64'130.00")

    def test_missing_year_to_date_counts_as_zero(self):
        d = prepared(earnings=[{"salary_component": "Basic Salary", "amount": 8000.0}])
        self.assertEqual(d["year_to_date_gross"], "0.00")
        self.assertEqual(d["year_to_date_net"], "0.00")


class TemplateTest(unittest.TestCase):
    """The template renders offline (jinja2, as the PDF job does in the container): every value it
    prints comes from prepare, so an invented slip renders without a site."""

    @classmethod
    def setUpClass(cls):
        try:
            import jinja2
        except ImportError:
            raise unittest.SkipTest("jinja2 not installed")
        path = os.path.join(os.path.dirname(__file__), "lohnabrechnung.html")
        with open(path, encoding="utf-8") as f:
            cls.template = jinja2.Template(f.read())

    def render(self, **over):
        return self.template.render(payslip_data=lambda doc: prepared(**over), doc={})

    def test_renders_the_header_and_the_blocks(self):
        html = self.render()
        for text in ("Lohnabrechnung", "Nina Muster", "•••••••••7897", "Verdienste", "Abzüge",
                     "Arbeitgeberbeiträge", "Jahr bis heute", "6'415.00", "64'130.00", "2957"):
            self.assertIn(text, html)

    def test_the_employee_s_block_holds_no_employer_row(self):
        html = self.render()
        abzuege = html.split("Abzüge")[1].split("Auszahlung")[0]
        self.assertNotIn("Employer", abzuege)

    def test_renders_a_bare_slip_without_an_employee(self):
        html = self.template.render(payslip_data=lambda doc: prepare(slip()), doc={})
        self.assertIn("Lohnabrechnung", html)


if __name__ == "__main__":
    unittest.main()
