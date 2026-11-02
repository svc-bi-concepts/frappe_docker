"""The payroll hand-over: the Swiss amounts per employee, for the trustee's 2026 filing.

One row per employee for the period, or per employee and month. The amounts are the slips' own (gross, net) and the
Salary Detail rows of each slip, summed by component, so the rows match the payslips.

The Lohnausweis view follows the ESTV Wegleitung for Form 11 (found in erp-hx7s): line 1 carries
every earning row of the slips, line 11 is line 8 less lines 9 and 10.1 (not the slips' net pay, which also
deducts Quellensteuer and KTG), and the KTG employee share goes into line 15 as a remark, for want of a line of
its own. The printed certificate shows whole francs (round half up, certificate_rounding; the certificate's own rounding is
erpnextswiss patch 0009, finance/patches/erpnextswiss); the view keeps rappen.

Pure Python, so the offline tests run without frappe (test_hand_over.py). The report
(bi_payroll/bi_payroll/report/payroll_hand_over) reads the submitted Salary Slips and passes them here.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby


def salary_slip_filters(company, start, end):
    """Submitted payroll slips only; imported history lacks hand-over insurance bases."""
    return {
        "company": company,
        "docstatus": 1,
        "swiss_bexio_id": ["is", "not set"],
        "start_date": ["between", [start, end]],
    }


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
# The earnings that keep a column of their own. Lohnausweis line 1 is wider: every earning row of the slip
# (the report tells hand_over() which Salary Detail rows sit in a slip's earnings table), per the Wegleitung's
# Ziffer 1 (the salary, Provisionen, any Zulage).
EARNINGS = {"basic": "Basic Salary", "commission": "Commission"}
EMPLOYER_SHARES = [key for key in COMPONENTS if key.endswith("_employer")]
AMOUNTS = ["gross", "net", "earnings", *EARNINGS, *COMPONENTS, "employer_total"]

# The Lohnausweis (Form 11) lines that the view fills, numbered as erpnextswiss's Salary Certificate numbers them
# (its fields 1 to 15, the same numbers as the form). Per the ESTV Wegleitung 2026: line 1 is every earning of the
# slip (Ziffer 1), line 11 is line 8 less the total of lines 9 and 10 (Ziffer 11) — the slips' own net is lower,
# it also deducts Quellensteuer and KTG. Line 15 (Bemerkungen) carries the KTG employee share as a remark
# (KTG_REMARK): KTG has no line of its own. A line not listed is not in payroll yet: docs/hrms.md has the full
# table. (column key, the form's line, the amounts summed into it; None keys: lohnausweis() computes the line)
LOHNAUSWEIS = [
    ("line_1", "1. Lohn", ["earnings"]),
    ("line_8", "8. Bruttolohn total", ["gross"]),
    (
        "line_9",
        "9. Beiträge AHV/IV/EO/ALV/NBUV",
        ["ahv_employee", "alv_employee", "nbu_employee"],
    ),
    ("line_10_1", "10.1 Ordentliche Beiträge berufliche Vorsorge", ["bvg_employee"]),
    ("line_11", "11. Nettolohn", None),
    ("line_12", "12. Quellensteuerabzug", ["quellensteuer"]),
    ("line_15", "15. Bemerkungen", None),
]
# The line-15 remark for the KTG employee share; the amount formats per the flag: rappen in the view, whole
# francs for the print or the export.
KTG_REMARK = "KTG-Beitrag Arbeitnehmer CHF {0}"


def hand_over(slips, lines, by_month, earnings=None):
    """slips: dicts with name, employee, employee_name, ahv_number, month, gross_pay, net_pay (submitted slips).
    lines: {slip name: {component name: amount}}, the Salary Detail rows of each slip summed by component.
    earnings: {slip name: {component name, ...}}, the rows that sit in a slip's earnings table; their sum is
    Lohnausweis line 1, so it names every earning, not only Basic Salary.
    by_month: one row per employee and month, else one row per employee for the whole period.
    """
    key_of = {name: key for key, name in {**EARNINGS, **COMPONENTS}.items()}
    earning_names = earnings or {}
    rows = {}
    for slip in slips:
        month = slip["month"] if by_month else None
        row = rows.get((slip["employee"], month))
        if row is None:
            row = rows[(slip["employee"], month)] = dict(
                employee=slip["employee"],
                employee_name=slip["employee_name"],
                ahv_number=slip["ahv_number"],
                month=month,
                slips=0,
                **{key: 0 for key in AMOUNTS},
            )
        row["slips"] += 1
        row["gross"] += slip["gross_pay"]
        row["net"] += slip["net_pay"]
        slip_earnings = earning_names.get(slip["name"], ())
        for component, amount in lines.get(slip["name"], {}).items():
            if component in key_of:
                row[key_of[component]] += amount
            if component in slip_earnings:
                row["earnings"] += amount
    for row in rows.values():
        row["employer_total"] = sum(row[key] for key in EMPLOYER_SHARES)
        for key in AMOUNTS:
            row[key] = round(row[key], 2)
    return sorted(rows.values(), key=lambda row: (row["employee"], row["month"] or 0))


def certificate_rounding(amount):
    """The whole francs of the printed certificate, round half up (the form is filled in francs, not rappen).
    The view keeps rappen; this runs for the print or the export flag only."""
    return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def lohnausweis(rows, francs=False):
    """The hand-over rows, with the amounts on the Lohnausweis lines instead of the components: one row per row.
    Line 11 is computed per the Wegleitung (line 8 less lines 9 and 10.1), line 15 remarks the KTG employee share.
    francs: whole francs for the printed certificate or the export (round half up); the view keeps rappen.
    """
    view = []
    for row in rows:
        line = {
            **{
                key: row[key]
                for key in ("employee", "employee_name", "ahv_number", "month", "slips")
            },
            **{
                column: round(sum(row[key] for key in keys), 2)
                for column, _, keys in LOHNAUSWEIS
                if keys
            },
        }
        # Ziffer 11: Ziffer 8 less the total of Ziffer 9 and 10 — not the slips' net pay, which also deducts
        # Quellensteuer (Ziffer 12) and KTG.
        line["line_11"] = round(line["line_8"] - line["line_9"] - line["line_10_1"], 2)
        # Ziffer 15 (Bemerkungen): the KTG employee share has no line of its own.
        if row["ktg_employee"]:
            amount = (
                certificate_rounding(row["ktg_employee"])
                if francs
                else f"{row['ktg_employee']:.2f}"
            )
            line["line_15"] = KTG_REMARK.format(amount)
        else:
            line["line_15"] = ""
        if francs:
            # The printed form's arithmetic must hold on its own francs: line 11 from the printed lines.
            for column, _, _ in LOHNAUSWEIS:
                if column not in ("line_11", "line_15"):
                    line[column] = certificate_rounding(line[column])
            line["line_11"] = line["line_8"] - line["line_9"] - line["line_10_1"]
        view.append(line)
    return view


# The Quellensteuer return: the slips' own tax, per canton and per month or quarter. The period is the report's filter.
QST_PERIODS = ("month", "quarter")


def qst_rows(slips, period):
    """The Quellensteuer view. slips: dicts with name, employee, employee_name, month, gross_pay, swiss_qst_canton,
    swiss_qst_code, swiss_qst_amount (submitted slips that carry a code: the report filters them).
    period: "month" or "quarter". One row per canton, period and employee, then the canton's total for the period.
    The rate is the tax over the gross, the effective rate of the slips: the tariff's own rate is not on the slip.
    """
    if period not in QST_PERIODS:
        raise ValueError(f"period must be one of {QST_PERIODS}, not {period!r}")

    def period_of(slip):
        return slip["month"] if period == "month" else (slip["month"] - 1) // 3 + 1

    rows = {}
    for slip in slips:
        # the code is part of the key: an employee whose code changed in the period has a row for each
        key = (
            slip["swiss_qst_canton"] or "",
            period_of(slip),
            slip["employee"],
            slip["swiss_qst_code"] or "",
        )
        row = rows.get(key)
        if row is None:
            row = rows[key] = dict(
                canton=key[0],
                period=key[1],
                employee=slip["employee"],
                employee_name=slip["employee_name"],
                tariff_code=key[3],
                slips=0,
                gross=0,
                amount=0,
            )
        row["slips"] += 1
        row["gross"] += slip["gross_pay"]
        row["amount"] += slip["swiss_qst_amount"]

    ordered = sorted(
        rows.values(),
        key=lambda row: (
            row["canton"],
            row["period"],
            row["employee"],
            row["tariff_code"],
        ),
    )
    out = []
    for (canton, number), group in groupby(
        ordered, key=lambda row: (row["canton"], row["period"])
    ):
        group = list(group)
        out += [_finish_qst(row) for row in group]
        out.append(
            _finish_qst(
                dict(
                    canton=canton,
                    period=number,
                    employee="",
                    employee_name="Total",
                    tariff_code="",
                    slips=sum(row["slips"] for row in group),
                    gross=sum(row["gross"] for row in group),
                    amount=sum(row["amount"] for row in group),
                )
            )
        )
    return out


def _finish_qst(row):
    row["gross"] = round(row["gross"], 2)
    row["amount"] = round(row["amount"], 2)
    row["rate"] = round(row["amount"] / row["gross"] * 100, 2) if row["gross"] else 0
    return row


# The Jahresdeklarationen: the wage sums per insurance for the year, as the funds ask them. The wage bases are the
# slips' own: the AHV and the FAK and KTG bases are the gross, the ALV and UVG/NBU bases stop at the ceiling the slip
# was computed with, and the BVG salary is the one of the slip's assignment, from the threshold on, the age band.
ANNUAL_BASES = [
    "ahv_wage",
    "alv_wage",
    "uvg_wage",
    "bvg_salary",
    "bvg_coordinated",
    "fak_wage",
    "ktg_wage",
]
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
    return (
        slip["bvg_insured_yearly"] / 12,
        max(0, slip["bvg_insured_yearly"] - slip["swiss_bvg_coordination"]) / 12,
    )


def annual_rows(slips, lines, year):
    """The Jahresdeklarationen: one row per employee for the year, and the total. slips: dicts with name, employee,
    employee_name, ahv_number, start_date, gross_pay, swiss_alv_ceiling, swiss_uvg_max, swiss_bvg_threshold,
    swiss_bvg_coordination, bvg_insured_yearly (the slip's assignment). lines: {slip name: {component name: amount}}, as
    in hand_over. A slip of another year is left out; an employee who left in the year has the months he was paid.
    """
    key_of = {name: key for key, name in COMPONENTS.items()}
    rows = {}
    for slip in slips:
        if slip["start_date"].year != year:
            continue
        gross = slip["gross_pay"]
        row = rows.get(slip["employee"])
        if row is None:
            row = rows[slip["employee"]] = dict(
                employee=slip["employee"],
                employee_name=slip["employee_name"],
                ahv_number=slip["ahv_number"],
                slips=0,
                **{key: 0 for key in ANNUAL_BASES},
                **{key: 0 for key in ANNUAL_CONTRIBUTIONS},
            )
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
    total = dict(
        employee="",
        employee_name="Total",
        ahv_number="",
        slips=sum(r["slips"] for r in out),
    )
    for key in ANNUAL_BASES + ANNUAL_CONTRIBUTIONS:
        total[key] = sum(row[key] for row in out)
    out.append(_finish_annual(total))
    return out


def _finish_annual(row):
    for key in ANNUAL_BASES + ANNUAL_CONTRIBUTIONS:
        row[key] = round(row[key], 2)
    return row
