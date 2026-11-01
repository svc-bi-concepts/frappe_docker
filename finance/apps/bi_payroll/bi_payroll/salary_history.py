"""Store historical bexio paystubs verbatim, without running HRMS payroll hooks.

Validation helpers are pure Python so their boundaries can be tested offline.
"""

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

try:
    import frappe
except ImportError:
    frappe = None


def _whitelist(function):
    return frappe.whitelist(methods=["POST"])(function) if frappe else function


def history_key(value):
    """Reject empty keys before querying, especially when removing a snapshot."""
    if (
        not isinstance(value, str)
        or not value.startswith("paystub-")
        or value != value.strip()
    ):
        raise ValueError("bexio_id must start with paystub-")
    return value


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("invalid totals or row amount")
    return amount


def validate_slip(slip, employee_exists, component_lookup, bexio_exists):
    """Validate a paystub payload before any database write."""
    key = history_key(slip.get("bexio_id"))
    if bexio_exists(key):
        raise ValueError("exists")
    try:
        start = date.fromisoformat(str(slip["start_date"])[:10])
        end = date.fromisoformat(str(slip["end_date"])[:10])
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid period") from None
    if (
        start.day != 1
        or (start.year, start.month)
        != (
            end.year,
            end.month,
        )
        or end.day != calendar.monthrange(end.year, end.month)[1]
    ):
        raise ValueError("period must be one calendar month")
    if end > date(2026, 12, 31):
        raise ValueError("history slips must end by 2026-12-31")
    if not employee_exists(slip.get("employee")):
        raise ValueError("unknown Employee")
    normalized_rows = {}
    for field, component_type in (
        ("earnings", "Earning"),
        ("deductions", "Deduction"),
    ):
        if not isinstance(slip.get(field), list):
            raise ValueError(f"{field} must be a list")
        normalized_rows[field] = []
        for row in slip[field]:
            if not isinstance(row, dict):
                raise ValueError("salary row must be an object")
            component = component_lookup(row.get("salary_component"))
            if not component:
                raise ValueError("unknown Salary Component")
            if component["type"] != component_type:
                raise ValueError(f"Salary Component must be an {component_type}")
            default_excluded = component["do_not_include_in_total"]
            excluded = row.get("do_not_include_in_total", default_excluded)
            if excluded not in (0, 1):
                raise ValueError("do_not_include_in_total must be 0 or 1")
            normalized_rows[field].append({**row, "do_not_include_in_total": excluded})
    try:
        gross = _amount(slip["gross_pay"])
        deduction = _amount(slip["total_deduction"])
        net = _amount(slip["net_pay"])
        _amount(slip["paid_out"])
        # Excluded rows are still stored: validate their amounts as well.
        for row in normalized_rows["earnings"] + normalized_rows["deductions"]:
            _amount(row["amount"])
        included_earnings = sum(
            (
                _amount(r["amount"])
                for r in normalized_rows["earnings"]
                if not r.get("do_not_include_in_total")
            ),
            Decimal(0),
        )
        included_deductions = sum(
            (
                _amount(r["amount"])
                for r in normalized_rows["deductions"]
                if not r.get("do_not_include_in_total")
            ),
            Decimal(0),
        )
    except (KeyError, TypeError, InvalidOperation):
        raise ValueError("invalid totals or row amount") from None
    if (
        included_earnings != gross
        or included_deductions != deduction
        or gross - deduction != net
    ):
        raise ValueError("salary rows do not match totals")
    return start, end, normalized_rows["earnings"], normalized_rows["deductions"]


@_whitelist
def load_slip(slip):
    import frappe
    from frappe import _

    frappe.only_for("System Manager")
    if not isinstance(slip, dict):
        frappe.throw(_("Slip must be a JSON object"))
    try:
        start, end, earnings, deductions = validate_slip(
            slip,
            lambda name: bool(name and frappe.db.exists("Employee", name)),
            lambda name: (
                frappe.db.get_value(
                    "Salary Component",
                    name,
                    ["type", "do_not_include_in_total"],
                    as_dict=True,
                )
                if name
                else None
            ),
            lambda key: frappe.db.exists("Salary Slip", {"swiss_bexio_id": key}),
        )
    except ValueError as exc:
        if str(exc) == "exists":
            return "exists"
        frappe.throw(_(str(exc)))

    employee = frappe.get_doc("Employee", slip["employee"])
    days = calendar.monthrange(end.year, end.month)[1]
    doc = frappe.get_doc(
        {
            "doctype": "Salary Slip",
            "employee": employee.name,
            "employee_name": employee.employee_name,
            "company": employee.company,
            "posting_date": end,
            "start_date": start,
            "end_date": end,
            "currency": "CHF",
            "exchange_rate": 1,
            "payroll_frequency": "Monthly",
            "total_working_days": days,
            "payment_days": days,
            "salary_structure": "",
            "gross_pay": slip["gross_pay"],
            "base_gross_pay": slip["gross_pay"],
            "total_deduction": slip["total_deduction"],
            "base_total_deduction": slip["total_deduction"],
            "net_pay": slip["net_pay"],
            "base_net_pay": slip["net_pay"],
            "rounded_total": slip["paid_out"],
            "base_rounded_total": slip["paid_out"],
            "is_proposed": 1,
            "swiss_bexio_id": slip["bexio_id"],
            "earnings": [
                {
                    "salary_component": r["salary_component"],
                    "amount": r["amount"],
                    "do_not_include_in_total": r["do_not_include_in_total"],
                }
                for r in earnings
            ],
            "deductions": [
                {
                    "salary_component": r["salary_component"],
                    "amount": r["amount"],
                    "do_not_include_in_total": r["do_not_include_in_total"],
                }
                for r in deductions
            ],
        }
    )
    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.insert()
    # HRMS on_submit posts accruals, notifications and benefit/loan ledgers. Historical salaries already
    # have Journal Entries, so submit the stored snapshot directly in this transaction instead.
    frappe.db.set_value(
        "Salary Slip",
        doc.name,
        {"docstatus": 1, "status": "Submitted"},
        update_modified=False,
    )
    frappe.db.sql(
        "update `tabSalary Detail` set docstatus = 1 where parent = %s and parenttype = 'Salary Slip'",
        doc.name,
    )
    return doc.name


@_whitelist
def remove_slip(bexio_id):
    import frappe
    from frappe import _

    frappe.only_for("System Manager")
    try:
        bexio_id = history_key(bexio_id)
    except ValueError as exc:
        frappe.throw(_(str(exc)))
    docname = frappe.db.get_value("Salary Slip", {"swiss_bexio_id": bexio_id}, "name")
    if not docname:
        frappe.throw(_("History Salary Slip not found"))
    doc = frappe.get_doc("Salary Slip", docname)
    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.cancel()
    frappe.delete_doc("Salary Slip", docname, ignore_permissions=True, force=True)
    return docname
