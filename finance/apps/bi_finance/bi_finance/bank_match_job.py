"""The frappe side of bank_match: read the open lines and documents, decide (bank_match), book what is certain.

run(bank_account, dry_run) reads the unreconciled Bank Transactions of an account, the open invoices and bills in its
currency, the unallocated Payment Entries and the company's Bank Transaction Rules, and decides each line. A live run
books the certain ones:

- a document match makes a Payment Entry against the invoice or bill (submitted, as payment_run.py makes its own) and
  reconciles the line with it; a Payment Entry match only reconciles the line with that entry;
- a rule match makes a submitted Journal Entry of the bank's GL account and the rule's account, and reconciles the line
  with it.

Nothing is submitted for revaluation here: monthly_revaluation makes an Exchange Rate Revaluation draft that a person
reviews and submits. A second live run finds no certain line left and books nothing.
"""

import json

import frappe
from bi_finance import bank_match
from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
    create_bank_entry_and_reconcile,
    reconcile_vouchers,
)
from frappe.utils import add_days, get_first_day, getdate, nowdate

# ERPNext's Rounding Loss Allowance: a balance at or below it counts as zero. Nothing to set on the revaluation (it is
# required, not defaulted), so 0 books no rounding line; ledgerdemain may change it here.
REVALUATION_ROUNDING_LOSS_ALLOWANCE = 0.0

LINE_FIELDS = [
    "name",
    "date",
    "deposit",
    "withdrawal",
    "description",
    "reference_number",
    "transaction_id",
    "end_to_end_id",
    "currency",
    "company",
]

# Bank Matching Settings (a single doctype, both off until ledgerdemain's go): enabled turns the matching on after each
# import and sync; rules_enabled lets it book the lines a Bank Transaction Rule takes (the rules are read either way).
SETTINGS = "Bank Matching Settings"


def run(bank_account, dry_run=True, from_date=None, to_date=None):
    """Decide the account's unreconciled lines; book them unless dry_run. Returns {decisions, summary, booked, errors}."""
    gl_account, company = frappe.db.get_value(
        "Bank Account", bank_account, ["account", "company"]
    )
    currency = frappe.db.get_value("Account", gl_account, "account_currency")

    filters = {"bank_account": bank_account, "docstatus": 1, "status": "Unreconciled"}
    if from_date and to_date:
        filters["date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["date"] = [">=", from_date]
    elif to_date:
        filters["date"] = ["<=", to_date]
    lines = frappe.get_all(
        "Bank Transaction", filters=filters, fields=LINE_FIELDS, order_by="date asc"
    )
    for line in lines:
        line["currency"] = currency
        line["bank_account"] = bank_account

    decisions = bank_match.decide(
        lines,
        _open_documents(company, currency),
        _open_payments(company, currency, gl_account),
        _rules(company),
    )
    report = {
        "decisions": decisions,
        "summary": bank_match.summary(decisions),
        "booked": 0,
        "errors": [],
    }
    if dry_run:
        return report

    by_name = {line["name"]: line for line in lines}
    rules_enabled = bool(frappe.db.get_single_value(SETTINGS, "rules_enabled"))
    for decision in decisions:
        if decision["state"] != "match":
            continue
        if decision["pass"] == "rule" and not rules_enabled:
            # a rule's line stays for a person until Bank Matching Settings allows the rule bookings
            continue
        try:
            _book(by_name[decision["line"]], decision, gl_account)
            frappe.db.commit()
            report["booked"] += 1
        except Exception as err:
            # one line's failure does not stop the others; the message carries no amount or name
            frappe.db.rollback()
            report["errors"].append(type(err).__name__)
    return report


def match_account(bank_account):
    """The job after an import or sync: a live run on the account. Returns the report of run()."""
    return run(bank_account, dry_run=False)


def match_after_import(bank_account):
    """match_account for the callers that have already committed their import: a failure is logged, never raised, so
    the import stands. Returns {booked, errors, summary}, {failed: True}, or {skipped: True} while Bank Matching Settings
    is off."""
    if not frappe.db.get_single_value(SETTINGS, "enabled"):
        return {"skipped": True}
    try:
        report = match_account(bank_account)
    except Exception:
        frappe.log_error(title="bank_match after import")
        return {"failed": True}
    return {
        "booked": report["booked"],
        "errors": len(report["errors"]),
        "summary": report["summary"],
    }


def _open_documents(company, currency, user=None):
    docs = []
    for doctype, party_field, direction in (
        ("Sales Invoice", "customer_name", "in"),
        ("Purchase Invoice", "supplier_name", "out"),
    ):
        reader = frappe.get_list if user else frappe.get_all
        filters = {
                "company": company,
                "docstatus": 1,
                "outstanding_amount": [">", 0],
                "currency": currency,
            }
        kwargs = {
            "filters": filters,
            "fields": [
                "name",
                party_field,
                "posting_date",
                "outstanding_amount",
                "currency",
            ],
        }
        if user:
            kwargs["user"] = user
        for inv in reader(doctype, **kwargs):
            docs.append(
                {
                    "doctype": doctype,
                    "name": inv.name,
                    "party_name": inv.get(party_field),
                    "direction": direction,
                    "amount": float(inv.outstanding_amount),
                    "currency": inv.currency,
                    "date": str(inv.posting_date),
                    "reference": (
                        bank_match.sales_invoice_reference(inv.name)
                        if doctype == "Sales Invoice"
                        else None
                    ),
                }
            )
    return docs


def _open_payments(company, currency, gl_account, user=None):
    # a Payment Entry with a clearance date is already reconciled with a line: it names no second one
    payments = []
    reader = frappe.get_list if user else frappe.get_all
    kwargs = {
        "filters": {
            "company": company,
            "docstatus": 1,
            "unallocated_amount": [">", 0],
            "clearance_date": ["is", "not set"],
        },
        "fields": [
            "name",
            "payment_type",
            "reference_no",
            "unallocated_amount",
            "posting_date",
            "party_name",
            "paid_from",
            "paid_to",
            "paid_from_account_currency",
            "paid_to_account_currency",
        ],
    }
    if user:
        kwargs["user"] = user
    for pe in reader("Payment Entry", **kwargs):
        if pe.payment_type == "Pay" and pe.paid_from == gl_account:
            direction, pe_currency = "out", pe.paid_from_account_currency
        elif pe.payment_type == "Receive" and pe.paid_to == gl_account:
            direction, pe_currency = "in", pe.paid_to_account_currency
        else:
            continue
        if pe_currency != currency:
            continue
        payments.append(
            {
                "doctype": "Payment Entry",
                "name": pe.name,
                "party_name": pe.party_name,
                "direction": direction,
                "reference": bank_match.key(pe.reference_no),
                "amount": float(pe.unallocated_amount),
                "currency": pe_currency,
                "date": str(pe.posting_date),
            }
        )
    return payments


def _rules(company, user=None):
    rules = []
    reader = frappe.get_list if user else frappe.get_all
    kwargs = {"filters": {"company": company}, "pluck": "name"}
    if user:
        kwargs["user"] = user
    for name in reader("Bank Transaction Rule", **kwargs):
        doc = frappe.get_doc("Bank Transaction Rule", name)
        rules.append(
            {
                "name": doc.name,
                "priority": doc.priority,
                "company": doc.company,
                "transaction_type": doc.transaction_type,
                "min_amount": doc.min_amount,
                "max_amount": doc.max_amount,
                "classify_as": doc.classify_as,
                "bank_entry_type": doc.bank_entry_type,
                "account": doc.account,
                "description_rules": [
                    {"check": r.check, "value": r.value} for r in doc.description_rules
                ],
            }
        )
    return rules


def _book(line, decision, gl_account):
    if decision["pass"] == "rule":
        return _book_rule(line, decision["target"], gl_account)
    doctype, name = decision["target"]
    amount = abs(float(line["deposit"] or 0) - float(line["withdrawal"] or 0))
    if doctype == "Payment Entry":
        return _reconcile(line["name"], "Payment Entry", name, amount)
    entry = _payment_entry_for(doctype, name, line, amount, gl_account)
    return _reconcile(line["name"], "Payment Entry", entry.name, amount)


def _payment_entry_for(doctype, name, line, amount, gl_account):
    """A submitted Payment Entry against one invoice or bill, dated on the line, as payment_run makes its own."""
    invoice = frappe.get_doc(doctype, name)
    receivable = doctype == "Sales Invoice"
    entry = frappe.get_doc(
        {
            "doctype": "Payment Entry",
            "payment_type": "Receive" if receivable else "Pay",
            "company": line["company"],
            "party_type": "Customer" if receivable else "Supplier",
            "party": invoice.customer if receivable else invoice.supplier,
            "posting_date": getdate(line["date"]),
            "paid_from": invoice.debit_to if receivable else gl_account,
            "paid_to": gl_account if receivable else invoice.credit_to,
            "paid_amount": amount,
            "received_amount": amount,
            "reference_no": line["reference_number"] or invoice.name,
            "reference_date": getdate(line["date"]),
            "remarks": "Bank line {}, matched by bank_match".format(line["name"]),
            "references": [
                {
                    "reference_doctype": doctype,
                    "reference_name": invoice.name,
                    "due_date": invoice.due_date,
                    "total_amount": invoice.grand_total,
                    "outstanding_amount": invoice.outstanding_amount,
                    "allocated_amount": amount,
                }
            ],
        }
    )
    entry.insert()
    entry.submit()
    return entry


def _book_rule(line, rule_name, gl_account):
    rule = frappe.get_doc("Bank Transaction Rule", rule_name)
    amount = abs(float(line["deposit"] or 0) - float(line["withdrawal"] or 0))
    deposit = float(line["deposit"] or 0) > 0
    # the bank's side and the rule's side, equal and opposite
    entries = [
        {
            "account": gl_account,
            "debit": amount if deposit else 0,
            "credit": 0 if deposit else amount,
        },
        {
            "account": rule.account,
            "debit": 0 if deposit else amount,
            "credit": amount if deposit else 0,
        },
    ]
    create_bank_entry_and_reconcile(
        line["name"],
        cheque_date=getdate(line["date"]),
        posting_date=getdate(line["date"]),
        cheque_no="",
        entries=entries,
        user_remark="Bank line {}, rule {}".format(line["name"], rule.name),
    )


def _reconcile(bank_transaction, doctype, name, amount):
    reconcile_vouchers(
        bank_transaction,
        json.dumps(
            [{"payment_doctype": doctype, "payment_name": name, "amount": amount}]
        ),
    )


def monthly_revaluation():
    """The scheduler's monthly job on the 1st: a draft Exchange Rate Revaluation per company for the month that ended."""
    month_end = add_days(get_first_day(nowdate()), -1)
    for company in frappe.get_all("Company", pluck="name"):
        revaluation_draft(company, month_end)


def revaluation_preview(company, posting_date):
    """What revaluation_draft would book at posting_date: each account with its gain or loss. Reads only, saves nothing."""
    doc = _revaluation(company, posting_date)
    return [
        {
            "account": acc.get("account"),
            "account_currency": acc.get("account_currency"),
            "gain_loss": acc.get("gain_loss"),
        }
        for acc in doc.get_accounts_data()
        if acc.get("gain_loss")
    ]


def _revaluation(company, posting_date):
    return frappe.get_doc(
        {
            "doctype": "Exchange Rate Revaluation",
            "company": company,
            "posting_date": posting_date,
            "rounding_loss_allowance": REVALUATION_ROUNDING_LOSS_ALLOWANCE,
        }
    )


def revaluation_draft(company, posting_date):
    """The Exchange Rate Revaluation of a company at posting_date, saved as a draft with its accounts (never submitted).

    ERPNext takes the accounts in a foreign currency and their rates from the Currency Exchange rows of the date. No
    draft is made when no account has a gain or loss that month, and an existing revaluation of the company at that date
    is left as it is. Returns the draft's name, or None.

    The 31.12 revaluation must use the ESTV year-end rate (Jahresendkurs): it is entered as a Currency Exchange row
    dated 31.12 before the January run, since the Currency Exchange rows are the only rate source here.
    """
    existing = frappe.db.get_value(
        "Exchange Rate Revaluation",
        {"company": company, "posting_date": posting_date, "docstatus": ["<", 2]},
    )
    if existing:
        return existing
    doc = _revaluation(company, posting_date)
    doc.fetch_and_calculate_accounts_data()
    if not doc.accounts:
        return None
    doc.insert()
    return doc.name
