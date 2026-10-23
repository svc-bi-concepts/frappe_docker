"""Customer Payment Behaviour: per customer, how they pay and what is overdue, for the Cash Forecast card.

The arithmetic is in bi_finance.cash_forecast.payment_behaviour; this report reads the books the way the cash
forecast does, so the two agree:
- paid: the Sales Invoices paid from the start date to the end date, the last payment date as the paid date
  (paid_history); the start defaults to a year back, as the forecast's customer history does;
- open: the Sales Invoices unpaid on the end date (open_documents), overdue when due before it.
"""

import datetime

import frappe
from bi_finance import cash_forecast as cf
from bi_finance.bi_finance.report.cash_flow_forecast import cash_flow_forecast as cff
from frappe import _
from frappe.utils import escape_html, getdate, nowdate


def columns(currency):
    def col(label, fieldname, fieldtype="Float", precision=None, width=120, options=None):
        column = {
            "label": _(label),
            "fieldname": fieldname,
            "fieldtype": fieldtype,
            "width": width,
        }
        if precision is not None:
            column["precision"] = precision
        if options:
            column["options"] = options
        return column

    return [
        col("Customer", "customer", "Link", width=200, options="Customer"),
        col("Paid invoices", "paid", "Int", width=110),
        col("Paid on time", "on_time_share", "Percent", precision=1, width=110),
        col("Median days late", "median_days_late", precision=1, width=130),
        col("Max days late", "max_days_late", "Int", width=110),
        col("Mean days late", "mean_days_late", "Int", width=120),
        col("Open overdue invoices", "open_overdue_count", "Int", width=150),
        col("Open overdue amount", "open_overdue_amount", "Currency", width=160, options=currency),
        col("Oldest overdue (days)", "oldest_overdue_days", "Int", width=160),
    ]


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    to_date = getdate(filters.to_date or nowdate())
    from_date = getdate(filters.from_date or to_date - datetime.timedelta(days=cff.LOOKBACK_DAYS))
    if from_date > to_date:
        frappe.throw(_("From Date must be before To Date"))
    currency = frappe.get_cached_value("Company", company, "default_currency")

    open_items = cff.open_documents(company, to_date, "Sales Invoice")
    history = cff.paid_history(company, to_date, "Sales Invoice", set(open_items), from_date, to_date)
    rows = cf.payment_behaviour(history, open_items, to_date)
    if filters.customer:
        rows = [row for row in rows if row["customer"] == filters.customer]

    message = "<br>".join(
        escape_html(m)
        for m in [
            _(
                "Paid invoices: paid by a Payment Entry from {0} to {1}, the last payment as the paid date. "
                "Days late: from the due date to that date; negative is paid early. Mean days late is the figure the "
                "cash forecast moves a customer's receipts by."
            ).format(from_date.isoformat(), to_date.isoformat()),
            _("Open overdue: Sales Invoices unpaid on {0} and due before it, oldest first in days.").format(
                to_date.isoformat()
            ),
        ]
    )
    return columns(currency), rows, message, None
