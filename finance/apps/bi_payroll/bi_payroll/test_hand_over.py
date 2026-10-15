"""Offline tests of the payroll hand-over rows, with an invented employee. No frappe needed:

    python3 -m unittest bi_payroll.test_hand_over

from this app's directory. The slips and their Salary Detail lines are made here, the way the report reads them.
"""

import datetime
import unittest

from bi_payroll.hand_over import (
    AMOUNTS,
    COMPONENTS,
    LOHNAUSWEIS,
    annual_rows,
    bvg_insured,
    certificate_rounding,
    hand_over,
    lohnausweis,
    qst_rows,
)


def slip(name, month, gross, net, employee="EMP-TEST-001"):
    return {
        "name": name,
        "employee": employee,
        "employee_name": "Test Employee",
        "ahv_number": "756.0000.0000.00",
        "month": month,
        "gross_pay": gross,
        "net_pay": net,
    }


# An invented month: gross 8000, the employee's deductions 424 AHV, 88 ALV, 300 BVG, 40 NBU, 35 KTG, so the net
# is 7113. The employer's shares are not in the net (the components are not in the slip's total).
LINES = {
    "SAL-1": {
        "Basic Salary": 8000,
        "AHV/IV/EO Employee": 424,
        "ALV Employee": 88,
        "BVG Employee": 300,
        "NBU Employee": 40,
        "KTG Employee": 35,
        "AHV/IV/EO Employer": 424,
        "ALV Employer": 88,
        "BVG Employer": 300,
        "UVG Employer": 30,
        "KTG Employer": 35,
        "FAK Employer": 60,
    },
}
MONTH_1 = slip("SAL-1", 1, 8000, 7113)


class OneRowPerEmployee(unittest.TestCase):
    def test_slips_of_the_year_add_up_to_one_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8000, 7113)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        (row,) = hand_over(slips, lines, by_month=False)
        self.assertEqual(row["slips"], 2)
        self.assertEqual(row["gross"], 16000)
        self.assertEqual(row["net"], 14226)
        self.assertEqual(row["ahv_employee"], 848)
        self.assertIsNone(row["month"])

    def test_each_employee_has_a_row(self):
        slips = [
            slip("SAL-1", 1, 8000, 7113),
            slip("SAL-3", 1, 5000, 4400, employee="EMP-TEST-002"),
        ]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-3": {}}
        rows = hand_over(slips, lines, by_month=False)
        self.assertEqual(
            [r["employee"] for r in rows], ["EMP-TEST-001", "EMP-TEST-002"]
        )
        self.assertEqual(rows[1]["ahv_employee"], 0)


class OneRowPerMonth(unittest.TestCase):
    def test_each_month_of_an_employee_has_its_own_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8100, 7200)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        rows = hand_over(slips, lines, by_month=True)
        self.assertEqual([r["month"] for r in rows], [1, 2])
        self.assertEqual([r["gross"] for r in rows], [8000, 8100])


class Components(unittest.TestCase):
    def test_each_swiss_component_lands_in_its_own_column(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["ahv_employee"], 424)
        self.assertEqual(row["alv_employee"], 88)
        self.assertEqual(row["bvg_employee"], 300)
        self.assertEqual(row["nbu_employee"], 40)
        self.assertEqual(row["ktg_employee"], 35)
        self.assertEqual(row["uvg_employer"], 30)
        self.assertEqual(row["fak_employer"], 60)

    def test_the_basic_salary_is_an_amount_of_its_own(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["basic"], 8000)

    def test_quellensteuer_is_zero_on_a_slip_without_its_row(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["quellensteuer"], 0)

    def test_quellensteuer_lands_in_its_column_from_the_fixture_component(self):
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"Quellensteuer Employee": 250})}
        (row,) = hand_over([MONTH_1], lines, by_month=False)
        self.assertEqual(row["quellensteuer"], 250)

    def test_basic_salary_and_unknown_components_are_not_columns(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertNotIn("Basic Salary", row)
        self.assertNotIn("basic_salary", row)

    def test_employer_total_is_the_employer_shares_only(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["employer_total"], 424 + 88 + 300 + 30 + 35 + 60)

    def test_the_components_are_the_fixture_names(self):
        self.assertEqual(COMPONENTS["ahv_employee"], "AHV/IV/EO Employee")
        self.assertEqual(COMPONENTS["fak_employer"], "FAK Employer")
        self.assertEqual(COMPONENTS["quellensteuer"], "Quellensteuer Employee")


class Lohnausweis(unittest.TestCase):
    # The earning rows of SAL-1, as the report passes them (the Salary Detail rows in the slip's
    # earnings table).
    EARNED = {"SAL-1": {"Basic Salary"}}

    def line(self, lines=LINES, earnings=None):
        (row,) = hand_over(
            [MONTH_1], lines, by_month=False, earnings=earnings or self.EARNED
        )
        (line,) = lohnausweis([row])
        return line

    def test_line_1_is_the_basic_salary(self):
        self.assertEqual(self.line()["line_1"], 8000)

    def test_line_1_is_every_earning_row(self):
        # invented: 8,000 salary and 2,000 commission in one month
        lines = {"SAL-1": {**LINES["SAL-1"], "Commission": 2000}}
        earnings = {"SAL-1": {"Basic Salary", "Commission"}}
        (row,) = hand_over(
            [slip("SAL-1", 1, 10000, 9113)], lines, by_month=False, earnings=earnings
        )
        self.assertEqual(row["commission"], 2000)
        self.assertEqual(row["earnings"], 10000)
        (line,) = lohnausweis([row])
        self.assertEqual(line["line_1"], 10000)

    def test_line_1_takes_any_zulage_component(self):
        # the Wegleitung's Ziffer 1: any Zulage is Lohn, not only the earnings with a column of their own
        lines = {"SAL-1": {**LINES["SAL-1"], "Nachtzulage": 120}}
        earnings = {"SAL-1": {"Basic Salary", "Nachtzulage"}}
        (row,) = hand_over(
            [slip("SAL-1", 1, 8120, 7233)], lines, by_month=False, earnings=earnings
        )
        self.assertEqual(row["basic"], 8000)
        (line,) = lohnausweis([row])
        self.assertEqual(line["line_1"], 8120)

    def test_line_1_stays_zero_without_earning_rows(self):
        # the report always passes them; a caller who cannot sees it here
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        (line,) = lohnausweis([row])
        self.assertEqual(line["line_1"], 0)

    def test_line_8_is_the_slips_gross(self):
        self.assertEqual(self.line()["line_8"], 8000)

    def test_line_11_is_line_8_less_line_9_and_line_10_1(self):
        # the Wegleitung's Ziffer 11: 8,000 - 552 - 300 = 7,148. The slip's net (7,113) is lower: it also
        # deducts the KTG (35), which is not on a line.
        line = self.line()
        self.assertEqual(line["line_11"], 8000 - 552 - 300)
        self.assertNotEqual(line["line_11"], 7113)

    def test_line_11_differs_from_the_net_on_a_slip_with_quellensteuer_and_ktg(self):
        # QST (250) and KTG (35) sit in the slip's net, not in line 11
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"Quellensteuer Employee": 250})}
        line = self.line(lines=lines)
        self.assertEqual(line["line_12"], 250)
        self.assertEqual(line["line_11"], 7148)
        (row,) = hand_over(
            [slip("SAL-1", 1, 8000, 6863)], lines, by_month=False, earnings=self.EARNED
        )
        self.assertEqual(row["net"], 6863)  # the slip's own net: QST and KTG deducted

    def test_line_9_is_the_employee_ahv_alv_and_nbu(self):
        self.assertEqual(self.line()["line_9"], 424 + 88 + 40)

    def test_line_10_1_is_the_employee_bvg(self):
        self.assertEqual(self.line()["line_10_1"], 300)

    def test_line_12_is_the_quellensteuer_employee_row(self):
        self.assertEqual(self.line()["line_12"], 0)
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"Quellensteuer Employee": 250})}
        self.assertEqual(self.line(lines=lines)["line_12"], 250)

    def test_line_15_remarks_the_ktg_employee_share(self):
        self.assertEqual(self.line()["line_15"], "KTG-Beitrag Arbeitnehmer CHF 35.00")

    def test_line_15_stays_empty_without_ktg(self):
        lines = {"SAL-1": {k: v for k, v in LINES["SAL-1"].items() if "KTG" not in k}}
        self.assertEqual(self.line(lines=lines)["line_15"], "")

    def test_the_rows_keep_the_employee_and_the_slip_count(self):
        line = self.line()
        self.assertEqual(
            (line["employee"], line["slips"], line["month"]), ("EMP-TEST-001", 1, None)
        )

    def test_one_row_per_hand_over_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8100, 7200)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        earnings = {"SAL-1": {"Basic Salary"}, "SAL-2": {"Basic Salary"}}
        rows = lohnausweis(hand_over(slips, lines, by_month=True, earnings=earnings))
        self.assertEqual([r["month"] for r in rows], [1, 2])
        self.assertEqual([r["line_8"] for r in rows], [8000, 8100])

    def test_every_summed_column_sums_amounts_the_rows_carry(self):
        for column, _, keys in LOHNAUSWEIS:
            if keys is None:
                continue  # line 11 and line 15 are computed, not summed
            with self.subTest(column=column):
                self.assertTrue(set(keys) <= set(AMOUNTS))


class CertificateRounding(unittest.TestCase):
    def test_half_rappen_rounds_up(self):
        self.assertEqual(certificate_rounding(7148.5), 7149)
        self.assertEqual(certificate_rounding(7148.4), 7148)
        self.assertEqual(certificate_rounding(35), 35)

    def test_the_francs_flag_rounds_the_lines_half_up(self):
        # invented: 8,000.50 gross, 424.40 AHV, so the printed lines round on their own
        lines = {
            "SAL-1": dict(
                LINES["SAL-1"], **{"Basic Salary": 8000.5, "AHV/IV/EO Employee": 424.4}
            )
        }
        (row,) = hand_over(
            [slip("SAL-1", 1, 8000.5, 7113)],
            lines,
            by_month=False,
            earnings={"SAL-1": {"Basic Salary"}},
        )
        (line,) = lohnausweis([row], francs=True)
        self.assertEqual(line["line_8"], 8001)
        self.assertEqual(line["line_9"], 552)  # 552.40 rounds down
        self.assertEqual(
            line["line_11"], 8001 - 552 - 300
        )  # the printed form's own arithmetic
        self.assertIsInstance(line["line_11"], int)
        self.assertEqual(line["line_15"], "KTG-Beitrag Arbeitnehmer CHF 35")

    def test_the_view_keeps_rappen(self):
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"AHV/IV/EO Employee": 424.4})}
        (row,) = hand_over(
            [MONTH_1], lines, by_month=False, earnings={"SAL-1": {"Basic Salary"}}
        )
        (line,) = lohnausweis([row])
        self.assertEqual(line["line_9"], 552.4)
        self.assertIsInstance(line["line_11"], float)
        self.assertEqual(line["line_15"], "KTG-Beitrag Arbeitnehmer CHF 35.00")


class Payslips(unittest.TestCase):
    def test_net_is_gross_less_the_employee_deductions(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        employee = sum(
            row[k]
            for k in [
                "ahv_employee",
                "alv_employee",
                "bvg_employee",
                "nbu_employee",
                "ktg_employee",
            ]
        )
        self.assertEqual(row["gross"] - employee, row["net"])

    def test_the_slips_net_is_taken_as_it_is(self):
        # the net is the slip's own, not recomputed here: a change of the slip shows in the report
        (row,) = hand_over([slip("SAL-1", 1, 8000, 7000)], LINES, by_month=False)
        self.assertEqual(row["net"], 7000)

    def test_amounts_are_rounded_to_centimes(self):
        lines = {"SAL-1": {"AHV/IV/EO Employee": 0.1 + 0.2}}
        (row,) = hand_over([MONTH_1], lines, by_month=False)
        self.assertEqual(row["ahv_employee"], 0.3)


def qst_slip(
    name,
    month,
    gross,
    amount,
    canton,
    code,
    employee="EMP-TEST-001",
    employee_name="Test Employee",
):
    return {
        "name": name,
        "employee": employee,
        "employee_name": employee_name,
        "month": month,
        "gross_pay": gross,
        "swiss_qst_canton": canton,
        "swiss_qst_code": code,
        "swiss_qst_amount": amount,
    }


# Two invented cantons: ZH with two employees in February, BE with one employee in April.
QST_SLIPS = [
    qst_slip("Q-1", 1, 8000, 400, "ZH", "T1"),
    qst_slip("Q-2", 2, 8000, 400, "ZH", "T1"),
    qst_slip(
        "Q-3",
        2,
        5000,
        100,
        "ZH",
        "T2",
        employee="EMP-TEST-002",
        employee_name="Test Two",
    ),
    qst_slip(
        "Q-4",
        4,
        6000,
        0,
        "BE",
        "B1",
        employee="EMP-TEST-003",
        employee_name="Test Three",
    ),
]


def find(rows, canton, period, employee):
    return next(
        r
        for r in rows
        if (r["canton"], r["period"], r["employee"]) == (canton, period, employee)
    )


class QuellensteuerByMonth(unittest.TestCase):
    def test_one_row_per_canton_month_and_employee_then_the_canton_total(self):
        # the cantons in alphabetical order, the months in order, the employees, the total last of each group
        rows = qst_rows(QST_SLIPS, "month")
        self.assertEqual(
            [(r["canton"], r["period"], r["employee"]) for r in rows],
            [
                ("BE", 4, "EMP-TEST-003"),
                ("BE", 4, ""),
                ("ZH", 1, "EMP-TEST-001"),
                ("ZH", 1, ""),
                ("ZH", 2, "EMP-TEST-001"),
                ("ZH", 2, "EMP-TEST-002"),
                ("ZH", 2, ""),
            ],
        )

    def test_the_row_carries_the_slip_base_code_and_tax(self):
        row = find(qst_rows(QST_SLIPS, "month"), "ZH", 1, "EMP-TEST-001")
        self.assertEqual(
            (row["employee"], row["tariff_code"], row["slips"]),
            ("EMP-TEST-001", "T1", 1),
        )
        self.assertEqual((row["gross"], row["amount"]), (8000, 400))
        self.assertEqual(row["rate"], 5.0)

    def test_the_canton_total_adds_its_rows(self):
        total = find(qst_rows(QST_SLIPS, "month"), "ZH", 2, "")
        self.assertEqual(total["employee_name"], "Total")
        self.assertEqual(
            (total["slips"], total["gross"], total["amount"]), (2, 13000, 500)
        )
        self.assertEqual(total["rate"], 3.85)

    def test_a_zero_tax_slip_is_listed_with_rate_zero(self):
        row = find(qst_rows(QST_SLIPS, "month"), "BE", 4, "EMP-TEST-003")
        self.assertEqual((row["amount"], row["rate"]), (0, 0))

    def test_the_canton_totals_of_each_month(self):
        totals = [r for r in qst_rows(QST_SLIPS, "month") if r["employee"] == ""]
        self.assertEqual(
            [(t["canton"], t["period"], t["amount"]) for t in totals],
            [("BE", 4, 0), ("ZH", 1, 400), ("ZH", 2, 500)],
        )


class QuellensteuerByQuarter(unittest.TestCase):
    def test_the_months_of_a_quarter_add_up_to_one_row(self):
        rows = qst_rows(QST_SLIPS, "quarter")
        zh = [
            r for r in rows if r["canton"] == "ZH" and r["employee"] == "EMP-TEST-001"
        ]
        self.assertEqual(len(zh), 1)
        self.assertEqual(
            (zh[0]["period"], zh[0]["slips"], zh[0]["gross"], zh[0]["amount"]),
            (1, 2, 16000, 800),
        )

    def test_the_quarter_total_of_a_canton(self):
        (total,) = (
            r
            for r in qst_rows(QST_SLIPS, "quarter")
            if r["canton"] == "ZH" and r["employee"] == ""
        )
        self.assertEqual(
            (total["period"], total["slips"], total["gross"], total["amount"]),
            (1, 3, 21000, 900),
        )
        self.assertEqual(total["rate"], 4.29)

    def test_april_is_the_second_quarter(self):
        rows = qst_rows(QST_SLIPS, "quarter")
        self.assertEqual(
            [
                (r["canton"], r["period"])
                for r in rows
                if r["employee"] == "EMP-TEST-003"
            ],
            [("BE", 2)],
        )

    def test_an_employee_whose_code_changed_has_a_row_for_each_code(self):
        slips = [
            qst_slip("Q-5", 1, 1000, 50, "ZH", "T1"),
            qst_slip("Q-6", 3, 1000, 60, "ZH", "T9"),
        ]
        rows = [r for r in qst_rows(slips, "quarter") if r["employee"]]
        self.assertEqual(
            [(r["tariff_code"], r["amount"]) for r in rows], [("T1", 50), ("T9", 60)]
        )

    def test_a_slip_with_a_code_but_no_canton_is_listed_first_not_a_crash(self):
        # an Employee with a code and no canton: the slip's canton is None, which must not break the sort
        slips = QST_SLIPS + [
            qst_slip(
                "Q-7",
                2,
                1000,
                50,
                None,
                "T3",
                employee="EMP-TEST-004",
                employee_name="Test Four",
            )
        ]
        rows = qst_rows(slips, "month")
        self.assertEqual(rows[0]["canton"], "")
        self.assertEqual(rows[0]["employee"], "EMP-TEST-004")

    def test_an_unknown_period_is_refused(self):
        with self.assertRaises(ValueError):
            qst_rows(QST_SLIPS, "year")


# The Jahresdeklarationen: invented employees with a 2026 assignment of 90000 insured yearly, the ALV ceiling 126000
# and the UVG/NBU maximum 148200 (the monthly parts 10500 and 12350). The coordination is 25725.
ANNUAL = dict(
    ceiling=126000, uvg_max=148200, threshold=22000, coordination=25725, insured=90000
)


def annual_slip(
    name,
    month,
    gross,
    employee="EMP-TEST-001",
    year=2026,
    age=40,
    insured=ANNUAL["insured"],
):
    return {
        "name": name,
        "employee": employee,
        "employee_name": "Test Employee",
        "ahv_number": "756.0000.0000.00",
        "start_date": datetime.date(year, month, 1),
        "gross_pay": gross,
        "net_pay": gross - 500,
        "swiss_alv_ceiling": ANNUAL["ceiling"],
        "swiss_uvg_max": ANNUAL["uvg_max"],
        "swiss_bvg_threshold": ANNUAL["threshold"],
        "swiss_bvg_coordination": ANNUAL["coordination"],
        "bvg_insured_yearly": insured,
        "age": age,
    }


def annual_lines(slip, insured_bvg=True):
    # the Salary Detail amounts the rows read: the contributions, the BVG Age, and a Quellensteuer row that the
    # Jahresdeklarationen leave out
    bvg = 50 if insured_bvg else 0
    return {
        "BVG Age": slip["age"],
        "AHV/IV/EO Employee": 100,
        "ALV Employee": 10,
        "BVG Employee": bvg,
        "BVG Employer": bvg,
        "Quellensteuer Employee": 999,
    }


def annual_fixture():
    slips = [annual_slip(f"A-{m}", m, 15000 if m == 6 else 8000) for m in range(1, 13)]
    slips.append(annual_slip("A-2025", 12, 9999, year=2025))
    # an employee who left after March: three slips, no insured salary (below the threshold: no BVG)
    slips += [
        annual_slip(f"B-{m}", m, 5000, employee="EMP-TEST-002", insured=0)
        for m in range(1, 4)
    ]
    lines = {
        s["name"]: annual_lines(s, insured_bvg=s["bvg_insured_yearly"] > 0)
        for s in slips
    }
    return slips, lines


class Jahresdeklarationen(unittest.TestCase):
    def setUp(self):
        self.slips, self.lines = annual_fixture()
        self.rows = annual_rows(self.slips, self.lines, 2026)

    def row(self, employee):
        return next(r for r in self.rows if r["employee"] == employee)

    def test_one_row_per_employee_and_a_total_last(self):
        self.assertEqual(
            [r["employee"] for r in self.rows], ["EMP-TEST-001", "EMP-TEST-002", ""]
        )
        self.assertEqual(self.rows[-1]["employee_name"], "Total")

    def test_a_slip_of_another_year_is_left_out(self):
        self.assertEqual(self.row("EMP-TEST-001")["slips"], 12)
        self.assertEqual(self.row("EMP-TEST-001")["ahv_wage"], 103000)

    def test_the_alv_wage_stops_at_the_ceiling_the_slip_was_computed_with(self):
        # 11 months at 8000 and June at 15000, capped at 126000 / 12 = 10500
        self.assertEqual(self.row("EMP-TEST-001")["alv_wage"], 11 * 8000 + 10500)
        self.assertEqual(self.row("EMP-TEST-001")["ahv_wage"], 11 * 8000 + 15000)

    def test_the_uvg_and_nbu_wage_stops_at_the_uvg_maximum(self):
        self.assertEqual(self.row("EMP-TEST-001")["uvg_wage"], 11 * 8000 + 12350)

    def test_the_bvg_salary_is_the_insured_salary_and_its_coordinated_part(self):
        employee = self.row("EMP-TEST-001")
        self.assertEqual(employee["bvg_salary"], 90000)
        self.assertEqual(
            employee["bvg_coordinated"], round(12 * (90000 - 25725) / 12, 2)
        )

    def test_an_employee_who_left_has_the_months_he_was_paid(self):
        left = self.row("EMP-TEST-002")
        self.assertEqual(
            (left["slips"], left["ahv_wage"], left["alv_wage"]), (3, 15000, 15000)
        )
        self.assertEqual(
            (left["bvg_salary"], left["bvg_coordinated"], left["bvg_employee"]),
            (0, 0, 0),
        )

    def test_the_contributions_are_the_slips_components_per_insurance(self):
        employee = self.row("EMP-TEST-001")
        self.assertEqual(employee["ahv_employee"], 12 * 100)
        self.assertEqual(employee["alv_employee"], 12 * 10)
        self.assertEqual(employee["bvg_employee"], 12 * 50)
        self.assertEqual(employee["bvg_employer"], 12 * 50)

    def test_the_quellensteuer_is_not_a_contribution_of_the_declarations(self):
        self.assertNotIn("quellensteuer", self.row("EMP-TEST-001"))

    def test_the_total_adds_the_employees(self):
        total = self.rows[-1]
        self.assertEqual(total["slips"], 15)
        self.assertEqual(total["ahv_wage"], 103000 + 15000)
        self.assertEqual(total["alv_wage"], 11 * 8000 + 10500 + 15000)
        self.assertEqual(total["ahv_employee"], 15 * 100)

    def test_the_fak_and_ktg_bases_are_the_gross(self):
        employee = self.row("EMP-TEST-001")
        self.assertEqual((employee["fak_wage"], employee["ktg_wage"]), (103000, 103000))


class BvgInsured(unittest.TestCase):
    slip = {
        "bvg_insured_yearly": 90000,
        "swiss_bvg_threshold": 22000,
        "swiss_bvg_coordination": 25725,
    }

    def test_an_insured_employee_in_the_age_band(self):
        self.assertEqual(
            bvg_insured(self.slip, {"BVG Age": 25}), (7500, round(64275 / 12, 10))
        )

    def test_the_age_band_is_25_to_64(self):
        self.assertEqual(bvg_insured(self.slip, {"BVG Age": 24}), (0, 0))
        self.assertEqual(bvg_insured(self.slip, {"BVG Age": 65}), (0, 0))
        self.assertEqual(bvg_insured(self.slip, {"BVG Age": 64})[0], 7500)

    def test_the_age_of_the_slip_wins_over_the_line(self):
        self.assertEqual(bvg_insured(dict(self.slip, age=64), {"BVG Age": 20})[0], 7500)
        self.assertEqual(bvg_insured(dict(self.slip, age=20), {"BVG Age": 40}), (0, 0))

    def test_without_a_slip_age_the_line_is_the_fallback(self):
        self.assertEqual(bvg_insured(dict(self.slip), {"BVG Age": 30})[0], 7500)
        self.assertEqual(bvg_insured(dict(self.slip), {}), (0, 0))

    def test_below_the_threshold_nobody_is_insured(self):
        slip = dict(self.slip, bvg_insured_yearly=21000)
        self.assertEqual(bvg_insured(slip, {"BVG Age": 40}), (0, 0))


if __name__ == "__main__":
    unittest.main()
