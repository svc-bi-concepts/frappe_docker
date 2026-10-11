"""Cash Conversion Cycle: DSO, DPO, DIO and CCC for every month since 2019, with rolling 12-month values.

The GL gives the balances and the flows, so a feed that is behind or unreconciled does not change the
figures. Each figure is per company currency (CHF). The arithmetic is in bi_finance.cash_conversion;
this report only reads the books:

- revenue: Income accounts (3xxx), credit minus debit
- purchases: Expense accounts 4xxx and 6xxx (goods, materials, services, other operating costs), without
  depreciation, round-off and the 69x financial result; cost of goods sold (the Cost of Goods Sold type,
  4200 in the KMU chart) is part of them
- cost of goods sold: accounts of the Cost of Goods Sold type
- receivables: Receivable accounts; payables: Payable accounts, without the payroll and expense claim
  payables of the company; stock: Stock accounts
- days to pay: from the Payment Entry references to the invoices (allocated amount, payment date less
  invoice date); the journal entries that allocate to an invoice are not counted

The days in the rolling values are those of the 12 months, so a leap February counts. The table lists the months
newest first, from the month the books start; the chart draws the last 24 of them. The message of the report gives
the formulas, the start of the books and says when there is no stock.
"""

import datetime
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, getdate, nowdate

from bi_finance import cash_conversion as cc
from bi_finance.cash_conversion import build, membership, month_ends, month_totals, running

HISTORY_FROM = datetime.date(2019, 1, 1)

# Credit balances and credit flows are shown positive: revenue and payables are negated.
SIGN = {"revenue": -1, "purchases": 1, "cogs": 1, "receivables": 1, "payables": -1, "inventory": 1}
FLOWS = ["revenue", "purchases", "cogs"]
BALANCES = ["receivables", "payables", "inventory"]


def columns():
    def col(label, fieldname, fieldtype="Float", precision=2, width=110):
        column = {"label": _(label), "fieldname": fieldname, "fieldtype": fieldtype, "width": width}
        if fieldtype == "Float":
            column["precision"] = precision
        return column

    return [
        col("Month", "month", "Date", width=120),
        col("Revenue", "revenue"),
        col("Purchases", "purchases"),
        col("Cost of Goods Sold", "cogs"),
        col("Receivables", "receivables"),
        col("Payables", "payables"),
        col("Stock", "inventory"),
        col("DSO (days)", "dso", precision=1, width=90),
        col("DIO (days)", "dio", precision=1, width=90),
        col("DPO (days)", "dpo", precision=1, width=90),
        col("CCC (days)", "ccc", precision=1, width=90),
        col("DSO 12 months", "dso_12", precision=1, width=110),
        col("DIO 12 months", "dio_12", precision=1, width=110),
        col("DPO 12 months", "dpo_12", precision=1, width=110),
        col("CCC 12 months", "ccc_12", precision=1, width=110),
        col("CCC change 12 months", "ccc_12_change", precision=1, width=140),
        col("Days to pay customers, month", "paid_days_customers", precision=1, width=170),
        col("Days to pay suppliers, month", "paid_days_suppliers", precision=1, width=170),
        col("Days to pay customers, 12 months", "paid_days_customers_12", precision=1, width=200),
        col("Days to pay suppliers, 12 months", "paid_days_suppliers_12", precision=1, width=200),
    ]


def payments(company, as_of, doctype, payment_type):
    """(paid on, invoiced on, allocated amount) of each reference of a submitted Payment Entry to an invoice."""
    found = frappe.db.sql(
        """select pe.posting_date, inv.posting_date, per.allocated_amount
        from `tabPayment Entry Reference` per
        join `tabPayment Entry` pe on pe.name = per.parent
        join `tab{doctype}` inv on inv.name = per.reference_name
        where per.reference_doctype = %s and pe.payment_type = %s and pe.company = %s
            and pe.docstatus = 1 and inv.docstatus = 1 and pe.posting_date <= %s""".format(doctype=doctype),
        (doctype, payment_type, company, as_of),
    )
    return [(paid, invoiced, flt(amount)) for paid, invoiced, amount in found]


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    as_of = getdate(filters.as_of_date or nowdate())
    months = month_ends(HISTORY_FROM, as_of)

    payables_excluded = {
        frappe.get_cached_value("Company", company, field)
        for field in ("default_payroll_payable_account", "default_expense_claim_payable_account")
    }
    accounts = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        fields=["name", "root_type", "account_type", "account_number"],
    )
    groups = membership(accounts, payables_excluded)

    # Movements per group, signed, from the GL in company currency (the debit and credit columns).
    postings = defaultdict(list)
    if groups:
        entries = frappe.db.sql(
            """select account, posting_date, sum(debit - credit)
            from `tabGL Entry`
            where company = %s and is_cancelled = 0 and posting_date <= %s and account in %s
            group by account, posting_date""",
            (company, as_of, tuple(groups)),
        )
        for account, posted, amount in entries:
            for group in groups[account]:
                postings[group].append((posted, flt(amount) * SIGN[group]))

    flows, balances = {}, {}
    for group in FLOWS:
        flows[group] = month_totals(postings[group], HISTORY_FROM, len(months))[1]
    for group in BALANCES:
        before, per_month = month_totals(postings[group], HISTORY_FROM, len(months))
        balances[group] = running(before, per_month)

    customers = payments(company, as_of, "Sales Invoice", "Receive")
    suppliers = payments(company, as_of, "Purchase Invoice", "Pay")
    rows = build(months, flows, balances, customers, suppliers)

    messages = [
        _("DSO = receivables / revenue x days; DPO = payables / purchases x days; DIO = stock / cost of goods sold x days; "
          "CCC = DSO + DIO - DPO. The 12-month values use the 12 months to the row; days to pay are from the Payment Entries."),
    ]
    if rows:
        # the table starts at the first month with a posting: the months before it are all zero and are left out
        messages.append(_("The books start in {0}: the months before it have no entries and are left out.").format(
            rows[-1]["month"].strftime("%b %Y")))
    if months and all(balance == 0 for balance in balances["inventory"]):
        messages.append(_("No stock in any month: DIO is 0 days."))
    message = "<br>".join(escape_html(m) for m in messages)

    # The chart draws the last 24 months, whatever the table shows; latest_only (the number cards) is the table's newest row
    chart_rows = cc.shown(rows)
    chart = {
        "data": {
            "labels": [cc.month_label(row["month"]) for row in chart_rows],
            "datasets": [
                {"name": _("DSO 12 months"), "values": [row["dso_12"] for row in chart_rows]},
                {"name": _("DPO 12 months"), "values": [row["dpo_12"] for row in chart_rows]},
                {"name": _("DIO 12 months"), "values": [row["dio_12"] for row in chart_rows]},
                {"name": _("CCC 12 months"), "values": [row["ccc_12"] for row in chart_rows]},
            ],
        },
        "type": "line",
    }

    if cint(filters.latest_only):
        rows = rows[:1]
    return columns(), rows, message, chart
