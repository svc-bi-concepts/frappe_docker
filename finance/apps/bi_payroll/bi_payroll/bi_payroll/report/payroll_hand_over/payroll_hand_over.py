"""Payroll hand-over: the Swiss amounts per employee for a year, or for one month, for the trustee's filing.

The rows are read from the submitted Salary Slips of the company and their Salary Detail rows, so they are the
payslips' own amounts. Export them from the report (Excel or CSV) and send them to the trustee; the payroll data
stays out of the repository (finance/docs/hrms.md).

The view 'Lohnausweis' sums the same rows into the form's lines (hand_over.LOHNAUSWEIS). It is not the certificate:
the lines not in payroll are not in it, and the mapping is checked against the 2026 form before use
(finance/docs/hrms.md, step 6).

The view 'Quellensteuer Abrechnung' lists the withholding per canton and per month or quarter (hand_over.qst_rows), for
the cantonal return. The view 'Jahresdeklarationen' gives the year's wage sums per insurance (hand_over.annual_rows),
for the AHV, UVG, BVG, FAK and KTG declarations; it is always the whole year, whatever the month.
"""

import calendar
import datetime

import frappe
from frappe import _
from frappe.utils import flt

from bi_payroll.hand_over import (
    ANNUAL_BASES, ANNUAL_CONTRIBUTIONS, COMPONENTS, EMPLOYER_SHARES, LOHNAUSWEIS, annual_rows, hand_over, lohnausweis,
    qst_rows,
)

HAND_OVER = "Hand-over"
LOHNAUSWEIS_VIEW = "Lohnausweis"
QST_VIEW = "Quellensteuer Abrechnung"
ANNUAL_VIEW = "Jahresdeklarationen"
# the labels of the wage bases of the Jahresdeklarationen; the contributions take the components' names
ANNUAL_BASE_LABELS = {
    "ahv_wage": "AHV/IV/EO wage (gross)",
    "alv_wage": "ALV wage (to the ceiling)",
    "uvg_wage": "UVG and NBU insured wage (to the maximum)",
    "bvg_salary": "BVG insured salary (yearly, per month)",
    "bvg_coordinated": "BVG coordinated salary",
    "fak_wage": "FAK wage (gross)",
    "ktg_wage": "KTG wage (gross)",
}


def execute(filters=None):
    filters = frappe._dict(filters or {})
    year = int(filters.year)
    view = filters.view or HAND_OVER
    annual = view == ANNUAL_VIEW
    period = (filters.period or "Month").lower()
    # the whole year for the annual view, the whole quarter for the quarterly return: a month filter would cut them
    whole = annual or (view == QST_VIEW and period == "quarter")
    month = int(filters.month) if filters.month and not whole else None
    if month:
        start = datetime.date(year, month, 1)
        end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    else:
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    columns_of = {
        HAND_OVER: lambda: columns(bool(month)),
        LOHNAUSWEIS_VIEW: lambda: lohnausweis_columns(bool(month)),
        QST_VIEW: lambda: qst_columns(period),
        ANNUAL_VIEW: annual_columns,
    }[view]

    slip_filters = {"company": filters.company, "docstatus": 1, "start_date": ["between", [start, end]]}
    if view == QST_VIEW:
        # the slips the cantonal return lists: those with a tariff code, whatever the tax
        slip_filters["swiss_qst_code"] = ["is", "set"]
    slips = frappe.get_all(
        "Salary Slip",
        filters=slip_filters,
        fields=[
            "name", "employee", "employee_name", "start_date", "gross_pay", "net_pay", "swiss_qst_canton",
            "swiss_qst_code", "swiss_qst_amount", "swiss_alv_ceiling", "swiss_uvg_max", "swiss_bvg_threshold",
            "swiss_bvg_coordination",
        ],
        order_by="start_date asc",
    )
    if not slips:
        return columns_of(), []

    employees = list({s.employee for s in slips})
    employees_query = frappe.get_all(
        "Employee", filters={"name": ["in", employees]}, fields=["name", "social_security_number", "date_of_birth"]
    )
    ahv_numbers = {e.name: e.social_security_number or "" for e in employees_query}
    births = {e.name: e.date_of_birth for e in employees_query}
    lines = {}
    details = frappe.get_all(
        "Salary Detail",
        filters={"parenttype": "Salary Slip", "parent": ["in", [s.name for s in slips]]},
        fields=["parent", "salary_component", "amount"],
    )
    for d in details:
        slip_lines = lines.setdefault(d.parent, {})
        slip_lines[d.salary_component] = slip_lines.get(d.salary_component, 0) + flt(d.amount)

    slip_rows = [
        {
            "name": s.name, "employee": s.employee, "employee_name": s.employee_name,
            "ahv_number": ahv_numbers.get(s.employee, ""), "start_date": s.start_date, "month": s.start_date.month,
            "age": age_at_month_end(births.get(s.employee), s.start_date),
            "gross_pay": flt(s.gross_pay), "net_pay": flt(s.net_pay), "swiss_qst_canton": s.swiss_qst_canton,
            "swiss_qst_code": s.swiss_qst_code, "swiss_qst_amount": flt(s.swiss_qst_amount),
            "swiss_alv_ceiling": flt(s.swiss_alv_ceiling), "swiss_uvg_max": flt(s.swiss_uvg_max),
            "swiss_bvg_threshold": flt(s.swiss_bvg_threshold), "swiss_bvg_coordination": flt(s.swiss_bvg_coordination),
        }
        for s in slips
    ]
    if view == QST_VIEW:
        return qst_columns(period), qst_rows(slip_rows, period)
    if annual:
        bvg_insured_salaries(slip_rows, employees, end)
        return annual_columns(), annual_rows(slip_rows, lines, year)
    rows = hand_over(slip_rows, lines, bool(month))
    if view == LOHNAUSWEIS_VIEW:
        rows = lohnausweis(rows)
    return columns_of(), rows


def age_at_month_end(born, start_date):
    # the employee's age at the slip's month end: what the BVG Age formula computes (getdate(end_date)). The slip keeps
    # no age: HRMS's BVG Age row is statistical and stays off the slip, so the report computes it from the Employee.
    if not born:
        return None
    end = datetime.date(start_date.year, start_date.month, calendar.monthrange(start_date.year, start_date.month)[1])
    return end.year - born.year - ((end.month, end.day) < (born.month, born.day))


def bvg_insured_salaries(slips, employees, end):
    # the yearly BVG insured salary of each slip: the one of the employee's submitted Salary Structure Assignment that
    # began last on or before the slip's start (it is fixed per year, the slip keeps no copy of it)
    assignments = frappe.get_all(
        "Salary Structure Assignment",
        filters={"docstatus": 1, "employee": ["in", employees], "from_date": ["<=", end]},
        fields=["employee", "from_date", "swiss_bvg_insured_salary"],
        order_by="from_date asc",
    )
    for slip in slips:
        began = [
            flt(a.swiss_bvg_insured_salary) for a in assignments
            if a.employee == slip["employee"] and a.from_date <= slip["start_date"]
        ]
        slip["bvg_insured_yearly"] = began[-1] if began else 0


def columns(by_month):
    cols = [
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 130},
        {"label": _("Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
        {"label": _("AHV number"), "fieldname": "ahv_number", "fieldtype": "Data", "width": 130},
    ]
    if by_month:
        cols.append({"label": _("Month"), "fieldname": "month", "fieldtype": "Int", "width": 70})
    cols += [
        {"label": _("Slips"), "fieldname": "slips", "fieldtype": "Int", "width": 60},
        {"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 120},
    ]
    cols += [
        {"label": _(name), "fieldname": key, "fieldtype": "Currency", "width": 120}
        for key, name in COMPONENTS.items() if key not in EMPLOYER_SHARES
    ]
    cols += [{"label": _("Net"), "fieldname": "net", "fieldtype": "Currency", "width": 120}]
    cols += [
        {"label": _(name), "fieldname": key, "fieldtype": "Currency", "width": 120}
        for key, name in COMPONENTS.items() if key in EMPLOYER_SHARES
    ]
    cols += [{"label": _("Employer total"), "fieldname": "employer_total", "fieldtype": "Currency", "width": 120}]
    return cols


def lohnausweis_columns(by_month):
    # The view 'Lohnausweis': the form's lines (hand_over.LOHNAUSWEIS), not the components. Labels are the form's own.
    cols = [
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 130},
        {"label": _("Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
        {"label": _("AHV number"), "fieldname": "ahv_number", "fieldtype": "Data", "width": 130},
    ]
    if by_month:
        cols.append({"label": _("Month"), "fieldname": "month", "fieldtype": "Int", "width": 70})
    cols.append({"label": _("Slips"), "fieldname": "slips", "fieldtype": "Int", "width": 60})
    cols += [
        {"label": label, "fieldname": column, "fieldtype": "Currency", "width": 160}
        for column, label, _keys in LOHNAUSWEIS
    ]
    return cols


def qst_columns(period):
    # The view 'Quellensteuer Abrechnung': the canton's total after each canton and period, with no employee
    return [
        {"label": _("Canton"), "fieldname": "canton", "fieldtype": "Data", "width": 80},
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 130},
        {"label": _("Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
        {"label": _(period.capitalize()), "fieldname": "period", "fieldtype": "Int", "width": 80},
        {"label": _("Tariff code"), "fieldname": "tariff_code", "fieldtype": "Data", "width": 110},
        {"label": _("Slips"), "fieldname": "slips", "fieldtype": "Int", "width": 60},
        {"label": _("QST base (gross)"), "fieldname": "gross", "fieldtype": "Currency", "width": 130},
        {"label": _("Quellensteuer"), "fieldname": "amount", "fieldtype": "Currency", "width": 120},
        {"label": _("Rate (% of gross)"), "fieldname": "rate", "fieldtype": "Float", "width": 120},
    ]


def annual_columns():
    # The view 'Jahresdeklarationen': the wage bases, then the contributions per insurance, as the components' names
    cols = [
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 130},
        {"label": _("Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
        {"label": _("AHV number"), "fieldname": "ahv_number", "fieldtype": "Data", "width": 130},
        {"label": _("Slips"), "fieldname": "slips", "fieldtype": "Int", "width": 60},
    ]
    cols += [
        {"label": _(ANNUAL_BASE_LABELS[key]), "fieldname": key, "fieldtype": "Currency", "width": 150}
        for key in ANNUAL_BASES
    ]
    cols += [
        {"label": _(COMPONENTS[key]), "fieldname": key, "fieldtype": "Currency", "width": 120}
        for key in ANNUAL_CONTRIBUTIONS
    ]
    return cols
