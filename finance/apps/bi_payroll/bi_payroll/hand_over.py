"""The payroll hand-over: the Swiss amounts per employee, for the trustee's 2026 filing.

One row per employee for the period, or per employee and month. The amounts are the slips' own (gross, net) and the
Salary Detail rows of each slip, summed by component, so the rows match the payslips.

Pure Python, so the offline tests run without frappe (test_hand_over.py). The report
(bi_payroll/bi_payroll/report/payroll_hand_over) reads the submitted Salary Slips and passes them here.
"""

from itertools import groupby

# Column key -> the Salary Component's name in fixtures/salary_component.json. Quellensteuer is the component
# "Quellensteuer Employee" (erp-6rrh); a slip without that row leaves the column 0.
COMPONENTS = {
    "ahv_employee": "AHV/IV/EO Employee",
    "alv_employee": "ALV Employee",
    "bvg_employee": "BVG Employee",
    "nbu_employee": "NBU Employee",
    "ktg_employee": "KTG Employee",
    "quellensteuer": "Quellensteuer Employee",
    "ahv_employer": "AHV/IV/EO Employer",
    "alv_employer": "ALV Employer",
    "bvg_employer": "BVG Employer",
    "uvg_employer": "UVG Employer",
    "ktg_employer": "KTG Employer",
    "fak_employer": "FAK Employer",
}
# The earnings the rows carry besides gross: Lohnausweis line 1 is the salary and the commission (wage type 1218).
EARNINGS = {"basic": "Basic Salary", "commission": "Commission"}
EMPLOYER_SHARES = [key for key in COMPONENTS if key.endswith("_employer")]
AMOUNTS = ["gross", "net", *EARNINGS, *COMPONENTS, "employer_total"]

# The Lohnausweis (Form 11) lines that the rows fill, numbered as erpnextswiss's Salary Certificate numbers them
# (its fields 1 to 15, the same numbers as the form). A line not listed is not in payroll yet: docs/hrms.md has the
# full table. KTG is not on a line here; its own column keeps it visible until the trustee says where it goes.
# (column key, the form's line, the amounts summed into it)
LOHNAUSWEIS = [
    ("line_1", "1. Lohn", ["basic", "commission"]),
    ("line_8", "8. Bruttolohn total", ["gross"]),
    ("line_9", "9. Beiträge AHV/IV/EO/ALV/NBUV", ["ahv_employee", "alv_employee", "nbu_employee"]),
    ("line_10_1", "10.1 Ordentliche Beiträge berufliche Vorsorge", ["bvg_employee"]),
    ("line_11", "11. Nettolohn", ["net"]),
    ("line_12", "12. Quellensteuerabzug", ["quellensteuer"]),
    ("ktg_employee", "KTG Employee (not on a line: to check)", ["ktg_employee"]),
]


def hand_over(slips, lines, by_month):
    """slips: dicts with name, employee, employee_name, ahv_number, month, gross_pay, net_pay (submitted slips).
    lines: {slip name: {component name: amount}}, the Salary Detail rows of each slip summed by component.
    by_month: one row per employee and month, else one row per employee for the whole period."""
    key_of = {name: key for key, name in {**EARNINGS, **COMPONENTS}.items()}
    rows = {}
    for slip in slips:
        month = slip["month"] if by_month else None
        row = rows.get((slip["employee"], month))
        if row is None:
            row = rows[(slip["employee"], month)] = dict(
                employee=slip["employee"], employee_name=slip["employee_name"], ahv_number=slip["ahv_number"],
                month=month, slips=0, **{key: 0 for key in AMOUNTS})
        row["slips"] += 1
        row["gross"] += slip["gross_pay"]
        row["net"] += slip["net_pay"]
        for component, amount in lines.get(slip["name"], {}).items():
            if component in key_of:
                row[key_of[component]] += amount
    for row in rows.values():
        row["employer_total"] = sum(row[key] for key in EMPLOYER_SHARES)
        for key in AMOUNTS:
            row[key] = round(row[key], 2)
    return sorted(rows.values(), key=lambda row: (row["employee"], row["month"] or 0))


def lohnausweis(rows):
    """The hand-over rows, with the amounts summed by the Lohnausweis lines instead of the components: one row per row."""
    return [
        {
            **{key: row[key] for key in ("employee", "employee_name", "ahv_number", "month", "slips")},
            **{column: round(sum(row[key] for key in keys), 2) for column, _, keys in LOHNAUSWEIS},
        }
        for row in rows
    ]


# The Quellensteuer return: the slips' own tax, per canton and per month or quarter. The period is the report's filter.
QST_PERIODS = ("month", "quarter")


def qst_rows(slips, period):
    """The Quellensteuer view. slips: dicts with name, employee, employee_name, month, gross_pay, swiss_qst_canton,
    swiss_qst_code, swiss_qst_amount (submitted slips that carry a code: the report filters them).
    period: "month" or "quarter". One row per canton, period and employee, then the canton's total for the period.
    The rate is the tax over the gross, the effective rate of the slips: the tariff's own rate is not on the slip."""
    if period not in QST_PERIODS:
        raise ValueError(f"period must be one of {QST_PERIODS}, not {period!r}")

    def period_of(slip):
        return slip["month"] if period == "month" else (slip["month"] - 1) // 3 + 1

    rows = {}
    for slip in slips:
        # the code is part of the key: an employee whose code changed in the period has a row for each
        key = (slip["swiss_qst_canton"] or "", period_of(slip), slip["employee"], slip["swiss_qst_code"] or "")
        row = rows.get(key)
        if row is None:
            row = rows[key] = dict(
                canton=key[0], period=key[1], employee=slip["employee"], employee_name=slip["employee_name"],
                tariff_code=key[3], slips=0, gross=0, amount=0)
        row["slips"] += 1
        row["gross"] += slip["gross_pay"]
        row["amount"] += slip["swiss_qst_amount"]

    ordered = sorted(rows.values(), key=lambda row: (row["canton"], row["period"], row["employee"], row["tariff_code"]))
    out = []
    for (canton, number), group in groupby(ordered, key=lambda row: (row["canton"], row["period"])):
        group = list(group)
        out += [_finish_qst(row) for row in group]
        out.append(_finish_qst(dict(
            canton=canton, period=number, employee="", employee_name="Total", tariff_code="",
            slips=sum(row["slips"] for row in group), gross=sum(row["gross"] for row in group),
            amount=sum(row["amount"] for row in group))))
    return out


def _finish_qst(row):
    row["gross"] = round(row["gross"], 2)
    row["amount"] = round(row["amount"], 2)
    row["rate"] = round(row["amount"] / row["gross"] * 100, 2) if row["gross"] else 0
    return row


# The Jahresdeklarationen: the wage sums per insurance for the year, as the funds ask them. The wage bases are the
# slips' own: the AHV and the FAK and KTG bases are the gross, the ALV and UVG/NBU bases stop at the ceiling the slip
# was computed with, and the BVG salary is the one of the slip's assignment, from the threshold on, the age band.
ANNUAL_BASES = ["ahv_wage", "alv_wage", "uvg_wage", "bvg_salary", "bvg_coordinated", "fak_wage", "ktg_wage"]
ANNUAL_CONTRIBUTIONS = [key for key in COMPONENTS if key != "quellensteuer"]


def bvg_insured(slip, lines):
    """The BVG insured salaries of one month, as the BVG formulas compute them (swiss_rates, swiss_payroll): the yearly
    insured salary of the slip's assignment and its coordinated part, each a twelfth, when the employee is insured
    that month (the insured salary from the threshold on, age 25 to 64), else 0. The age is the slip's (the report
    computes it from the Employee's date of birth); the statistical BVG Age row of the lines is the fallback, for
    HRMS keeps that row off the slip."""
    age = slip.get("age") or lines.get("BVG Age") or 0
    if slip["bvg_insured_yearly"] < slip["swiss_bvg_threshold"] or not 25 <= age < 65:
        return 0, 0
    return slip["bvg_insured_yearly"] / 12, max(0, slip["bvg_insured_yearly"] - slip["swiss_bvg_coordination"]) / 12


def annual_rows(slips, lines, year):
    """The Jahresdeklarationen: one row per employee for the year, and the total. slips: dicts with name, employee,
    employee_name, ahv_number, start_date, gross_pay, swiss_alv_ceiling, swiss_uvg_max, swiss_bvg_threshold,
    swiss_bvg_coordination, bvg_insured_yearly (the slip's assignment). lines: {slip name: {component name: amount}}, as
    in hand_over. A slip of another year is left out; an employee who left in the year has the months he was paid."""
    key_of = {name: key for key, name in COMPONENTS.items()}
    rows = {}
    for slip in slips:
        if slip["start_date"].year != year:
            continue
        gross = slip["gross_pay"]
        row = rows.get(slip["employee"])
        if row is None:
            row = rows[slip["employee"]] = dict(
                employee=slip["employee"], employee_name=slip["employee_name"], ahv_number=slip["ahv_number"], slips=0,
                **{key: 0 for key in ANNUAL_BASES}, **{key: 0 for key in ANNUAL_CONTRIBUTIONS})
        slip_lines = lines.get(slip["name"], {})
        row["slips"] += 1
        row["ahv_wage"] += gross
        row["fak_wage"] += gross
        row["ktg_wage"] += gross
        row["alv_wage"] += min(gross, slip["swiss_alv_ceiling"] / 12)
        row["uvg_wage"] += min(gross, slip["swiss_uvg_max"] / 12)
        salary, coordinated = bvg_insured(slip, slip_lines)
        row["bvg_salary"] += salary
        row["bvg_coordinated"] += coordinated
        for component, amount in slip_lines.items():
            if component in key_of and key_of[component] in ANNUAL_CONTRIBUTIONS:
                row[key_of[component]] += amount

    out = []
    for row in sorted(rows.values(), key=lambda row: row["employee"]):
        out.append(_finish_annual(row))
    total = dict(employee="", employee_name="Total", ahv_number="", slips=sum(r["slips"] for r in out))
    for key in ANNUAL_BASES + ANNUAL_CONTRIBUTIONS:
        total[key] = sum(row[key] for row in out)
    out.append(_finish_annual(total))
    return out


def _finish_annual(row):
    for key in ANNUAL_BASES + ANNUAL_CONTRIBUTIONS:
        row[key] = round(row[key], 2)
    return row
