"""Creates the Bank Transaction Rules of bank_rules.RULES, once per company (migrate runs a patch once per site).

A rule that already exists in the company (by its name) is left as it is, so an edit in the Desk is kept. A rule whose
account number is not in the company is not created: the gap is logged and the migrate goes on.
"""

import frappe

from bi_finance.bank_rules import RULES


def execute():
    for company in frappe.get_all("Company", pluck="name"):
        for spec in RULES:
            _create(company, spec)


def _create(company, spec):
    if frappe.db.exists("Bank Transaction Rule", {"company": company, "rule_name": spec["rule_name"]}):
        return
    account = frappe.db.get_value(
        "Account", {"company": company, "account_number": spec["account_number"], "is_group": 0}, "name")
    if not account:
        frappe.log_error(title="bank rule {}".format(spec["rule_name"]), message="no account number {}".format(spec["account_number"]))
        return
    frappe.get_doc({
        "doctype": "Bank Transaction Rule",
        "rule_name": spec["rule_name"],
        "company": company,
        "transaction_type": spec["transaction_type"],
        "classify_as": "Bank Entry",
        "bank_entry_type": "Single Account",
        "account": account,
        "description_rules": [{"check": "Contains", "value": text} for text in spec["texts"]],
    }).insert(ignore_permissions=True)
