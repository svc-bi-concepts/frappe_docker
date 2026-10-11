"""Swiss setup of company BI Concepts, run inside the backend container by
swiss-setup.sh. Steps are named on the command line (coa, vat, fiscal,
fields, currencies, banks, gebuev, qrbill, payslip, host, payments, hr, payroll_accounts, lohnausweis); each one is re-runnable and skips what
already exists. `freeze <date> [--apply]` is a separate command: it closes the
books up to a date and is a dry run unless --apply is given."""
import csv
import datetime
import json
import os
import sys

import frappe
from frappe.utils import get_bench_path

COMPANY = "BI Concepts"

# erpnextswiss ships the KMU chart as a CSV. Its own importer (coa_importer)
# needs pandas, which the image does not have, so the CSV is read here.
COA_CSV = "apps/erpnextswiss/erpnextswiss/erpnextswiss/coa_import/accounts_template.csv"

# bexio's 42 VAT codes (/3.0/taxes), 1:1: (kind, bexio id, code, bexio name,
# rate, validity, active, account, deduction account). Kind S = sales, P =
# purchase. Source: private/bexio-mapping.md, "VAT codes". Until the full export
# has /3.0/taxes, the names come from the mapping's kind column and the accounts
# from its kind (sales 2202, purchase 1172, bexio's transitory accounts): `vat --check` compares
# them with bexio's own file once it exists. Accounts of the correction codes
# (1172-1174) and their 8.1 ids are not in the mapping and are unverified.
# Bezugsteuer books the same rate as deduction on 2202 (the liability), so the net is 0.
BEXIO_TAXES = [
    ("S", 16, "UN77", "Normalsatz", 7.7, "2017-10 bis 2023-12", True, "2202", None),
    ("S", 28, "UN81", "Normalsatz", 8.1, "ab 2023-07", True, "2202", None),
    ("S", 17, "UR25", "Reduzierter Satz", 2.5, "2017-10 bis 2023-12", True, "2202", None),
    ("S", 29, "UR26", "Reduzierter Satz", 2.6, "ab 2023-07", True, "2202", None),
    ("S", 18, "US37", "Sondersatz Beherbergung", 3.7, "ab 2017-10", False, "2202", None),
    ("S", 30, "US38", "Sondersatz Beherbergung", 3.8, "ab 2023-07", False, "2202", None),
    ("S", 3, "UEX", "Export, steuerbefreit", 0, "", True, "2202", None),
    ("S", 4, "ULA", "Ausland", 0, "", False, "2202", None),
    ("S", 5, "MEL", "Meldeverfahren", 0, "", False, "2202", None),
    ("S", 6, "UNO", "nicht optimiert", 0, "", False, "2202", None),
    ("S", 13, "SUB", "Subventionen", 0, "", False, "2202", None),
    ("S", 14, "SPE", "Spenden", 0, "", False, "2202", None),
    ("S", 15, "UO77", "optiert", 7.7, "2017-10", False, "2202", None),
    ("S", 31, "UO81", "optiert", 8.1, "ab 2023-07", False, "2202", None),
    ("S", 48, "U00", "Null-Satz", 0, "", False, "2202", None),
    ("P", 22, "VM77", "Normalsatz Material/DL", 7.7, "2017-10 bis 2023-12", True, "1172", None),
    ("P", 35, "VM81", "Normalsatz Material/DL", 8.1, "ab 2023-07", True, "1172", None),
    ("P", 8, "VM25", "Reduzierter Satz Material/DL", 2.5, "2017-10 bis 2023-12", True, "1172", None),
    ("P", 34, "VM26", "Reduzierter Satz Material/DL", 2.6, "ab 2023-07", True, "1172", None),
    ("P", 21, "VM37", "Sondersatz Beherbergung Material/DL", 3.7, "ab 2017-10", False, "1172", None),
    ("P", 36, "VM38", "Sondersatz Beherbergung Material/DL", 3.8, "ab 2023-07", False, "1172", None),
    ("P", 7, "VIM", "Import Material/DL, steuerbefreit", 0, "", True, "1172", None),
    ("P", 9, "ZOLLM", "Einfuhrsteuer Material/DL", 0, "", True, "1172", None),
    ("P", 33, "BZM81", "Bezugsteuer Material/DL", 8.1, "ab 2023-07", True, "1172", "2202"),
    ("P", 19, "BZM77", "Bezugsteuer Material/DL", 7.7, "ab 2017-10", False, "1172", "2202"),
    ("P", 24, "VB77", "Normalsatz Invest./Aufwand", 7.7, "2017-10 bis 2024-12", True, "1172", None),
    ("P", 38, "VB81", "Normalsatz Invest./Aufwand", 8.1, "ab 2023-01", True, "1172", None),
    ("P", 12, "VB25", "Reduzierter Satz Invest./Aufwand", 2.5, "2017-10 bis 2023-12", True, "1172", None),
    ("P", 37, "VB26", "Reduzierter Satz Invest./Aufwand", 2.6, "ab 2023-07", True, "1172", None),
    ("P", 23, "VB37", "Sondersatz Beherbergung Invest./Aufwand", 3.7, "ab 2017-10", False, "1172", None),
    ("P", 39, "VB38", "Sondersatz Beherbergung Invest./Aufwand", 3.8, "ab 2023-07", True, "1172", None),
    ("P", 47, "V00", "Null-Satz Invest./Aufwand", 0, "", True, "1172", None),
    ("P", 10, "VSF", "Import Invest./Aufwand, steuerbefreit", 0, "", True, "1172", None),
    ("P", 11, "ZOLLB", "Einfuhrsteuer Invest./Aufwand", 0, "", True, "1172", None),
    ("P", 32, "BZB81", "Bezugsteuer Invest./Aufwand", 8.1, "ab 2023-07", True, "1172", "2202"),
    ("P", 20, "BZB77", "Bezugsteuer Invest./Aufwand", 7.7, "2017-10 bis 2023-12", True, "1172", "2202"),
    ("P", 25, "VES", "Vorsteuerkorrektur nachträglich", 7.7, "2017-10 bis 2023-12", False, "1172", None),
    ("P", 26, "VEV", "Vorsteuerkorrektur Eigenverbrauch", 7.7, "2017-10 bis 2023-12", False, "1173", None),
    ("P", 27, "VKÜ", "Vorsteuerkorrektur Kürzung", 7.7, "2017-10 bis 2023-12", False, "1174", None),
    ("P", 40, "VES", "Vorsteuerkorrektur nachträglich", 8.1, "ab 2023-07", False, "1172", None),
    ("P", 41, "VEV", "Vorsteuerkorrektur Eigenverbrauch", 8.1, "ab 2023-07", False, "1173", None),
    ("P", 42, "VKÜ", "Vorsteuerkorrektur Kürzung", 8.1, "ab 2023-07", False, "1174", None),
]
# Code of the sales and purchase template that is the company default (8.1 %).
DEFAULT_SALES, DEFAULT_PURCHASE = 28, 35
TAX_DOCTYPES = ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template", "Item Tax Template")
# ERPNext's own templates (MWST/USt/VSt, the rate per period in the name) are
# replaced by the codes above and deleted by `vat`, once nothing refers to them.
FIXED_ACCOUNTS = ("2202", "1172", "1173", "1174", "2203")
# KMU account for USt payable (coa gives it the Tax type)
ACC_USt = "2200"

FISCAL_YEARS = range(2015, 2027)

BEXIO_DOCTYPES = [
    # (doctype, field to put bexio_id after)
    ("Customer", "customer_name"),
    ("Supplier", "supplier_name"),
    ("Contact", "last_name"),
    ("Address", "address_title"),
    ("Item", "item_name"),
    ("Account", "account_number"),
    ("Sales Invoice", "title"),
    ("Purchase Invoice", "title"),
    ("Journal Entry", "title"),
    ("Payment Entry", "payment_type"),
    # master data of the bexio import (finance-sg76)
    ("Customer Group", "customer_group_name"),
    ("Supplier Group", "supplier_group_name"),
    ("Item Price", "item_code"),
    ("Bank Account", "account_name"),
    # keys for the document import (finance-3qsp)
    ("Sales Order", "title"),
    ("Quotation", "title"),
    ("Delivery Note", "title"),
    ("Bank Transaction", "date"),
    # the bexio VAT codes, mapped by bexio id (erp-7rbc)
    ("Sales Taxes and Charges Template", "title"),
    ("Purchase Taxes and Charges Template", "title"),
    ("Item Tax Template", "title"),
    # the bexio files attached to Purchase Invoices, keyed by their bexio uuid (import_files.py)
    ("File", "attached_to_name"),
    # master data of the bexio payroll: the Employee (import_employees.py, erp-y909)
    ("Employee", "employee_name"),
]

# Currencies bexio books in, enabled by the `currencies` step. CHF is paid in
# 0.05 steps (Swiss cash rounding); the others keep the cent.
CURRENCIES = {"CHF": 0.05, "EUR": 0.01, "USD": 0.01, "GBP": 0.01,
              "BRL": 0.01, "JPY": 0.01, "CNY": 0.01, "PLN": 0.01}


# GeBüV (OR 958f, GeBüV art. 9-10): a posted record may not change or vanish
# without a trace. ERPNext v16 already refuses to delete a submitted document
# and versions changes to tracked doctypes; the gebuev step sets what is left.
GEBUEV_TRACKED = ["Account", "Customer", "Supplier", "Item", "Bank Account", "Company",
                  "Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry",
                  "Bank Transaction"]
# The one role that may delete on these doctypes; Administrator is not listed
# because it bypasses the role check.
GEBUEV_DELETE_ROLE = "Accounts Manager"


def say(msg):
    print(msg, flush=True)


def set_doctype_property(doctype, prop, value, property_type):
    # frappe 16 takes one args dict here; the positional form (doctype, fieldname, property, ...) raises
    # TypeError. fieldname stays None: a DocType property names no field.
    frappe.make_property_setter({
        "doctype": doctype,
        "doctype_or_field": "DocType",
        "fieldname": None,
        "property": prop,
        "value": value,
        "property_type": property_type,
    })


def delete_revokes(perms, keep):
    """perms: (role, permlevel, delete) rows, as the effective permissions of a
    doctype. Returns the (role, permlevel) pairs that may delete but are not in keep."""
    return [(role, level) for role, level, delete in perms if delete and role not in keep]


def parse_freeze(args):
    """args after `freeze`: a date and optionally --apply. Returns (date, apply)."""
    apply = "--apply" in args
    dates = [a for a in args if a != "--apply"]
    if len(dates) != 1 or len(args) != len(dates) + apply:
        sys.exit("usage: swiss-setup.sh freeze <YYYY-MM-DD> [--apply]")
    try:
        return datetime.date.fromisoformat(dates[0]).isoformat(), apply
    except ValueError:
        sys.exit(f"freeze: not a date: {dates[0]}")


def account(number):
    name = frappe.db.get_value(
        "Account", {"company": COMPANY, "account_number": number}, "name"
    )
    if not name:
        frappe.throw(f"Account {number} not found in {COMPANY}; run the coa step first")
    return name


def coa():
    # Replacing the chart is only safe while nothing has been booked.
    gl = frappe.db.count("GL Entry", {"company": COMPANY})
    if gl:
        say(f"STOP: {gl} GL entries for {COMPANY}; the chart can no longer be replaced")
        sys.exit(3)

    abbr = frappe.db.get_value("Company", COMPANY, "abbr")
    if frappe.db.exists("Account", f"1000 - Kasse - {abbr}"):
        say("coa: KMU chart already in place")
        return

    # The stock 'Switzerland ... VAT' templates point at the old accounts.
    for dt in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template", "Item Tax Template"):
        for name in frappe.get_all(dt, {"company": COMPANY, "title": ["like", "Switzerland %"]}, pluck="name"):
            frappe.delete_doc(dt, name)
            say(f"coa: deleted {dt} {name}")

    # Links from the company and the payment modes to the old accounts.
    for f in frappe.get_meta("Company").get_link_fields():
        if f.options == "Account":
            frappe.db.set_value("Company", COMPANY, f.fieldname, None)
    frappe.db.delete("Mode of Payment Account", {"company": COMPANY})
    frappe.db.commit()

    frappe.db.delete("Account", {"company": COMPANY})
    with open(f"{get_bench_path()}/{COA_CSV}", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row = {k: (v or "").strip() for k, v in row.items()}
        parent = f"{row['parent_account']} - {abbr}" if row["parent_account"] else ""
        acc = frappe.get_doc({
            "doctype": "Account",
            "account_name": row["account_name"],
            "company": COMPANY,
            "parent_account": parent,
            "account_number": row["account_number"],
            "is_group": int(row["is_group"]),
            "root_type": row["root_type"],
            "report_type": row["report_type"],
            "account_currency": row["account_currency"],
            "account_type": row["account_type"],
            "tax_rate": float(row["tax_rate"] or 0),
            "freeze_account": row["freeze_account"],
        })
        # roots have no parent, which the form marks mandatory
        acc.flags.ignore_mandatory = True
        acc.flags.ignore_root_company_validation = True
        acc.insert()
        default = row["company_default_account"]
        if default and frappe.get_meta("Company").has_field(default):
            frappe.db.set_value("Company", COMPANY, default, acc.name)
        elif default:
            say(f"coa: no Company field {default} (template default for {acc.name})")
    frappe.db.commit()

    # The template leaves 2200 without a type; tax templates need type Tax.
    frappe.db.set_value("Account", account(ACC_USt), "account_type", "Tax")
    for mop, number in (("Cash", "1000"),):
        if frappe.db.exists("Mode of Payment", mop):
            doc = frappe.get_doc("Mode of Payment", mop)
            doc.append("accounts", {"company": COMPANY, "default_account": account(number)})
            doc.save()
    frappe.db.commit()
    say(f"coa: {frappe.db.count('Account', {'company': COMPANY})} accounts")


def tax_title(code, rate, name):
    # "UN81 8.1% Normalsatz": the code first, so the name tells which bexio code it is
    return f"{code} {rate:g}% {name}"


def tax_rows(dt, kind, account_no, deduct_no, rate):
    acc = account(account_no)
    if dt == "Item Tax Template":
        return [{"tax_type": acc, "tax_rate": rate}]
    if dt == "Sales Taxes and Charges Template":
        return [{"charge_type": "On Net Total", "account_head": acc, "rate": rate}]
    rows = [{"category": "Total", "add_deduct_tax": "Add", "charge_type": "On Net Total",
             "account_head": acc, "rate": rate}]
    if deduct_no:
        # reverse charge: the same rate comes back as a deduction on 2202, net 0
        rows.append({"category": "Total", "add_deduct_tax": "Deduct", "charge_type": "On Net Total",
                     "account_head": account(deduct_no), "rate": rate})
    return rows


def row_key(dt):
    if dt == "Item Tax Template":
        return ("tax_type", "tax_rate")
    if dt == "Sales Taxes and Charges Template":
        return ("account_head", "rate", "description")
    return ("account_head", "rate", "add_deduct_tax", "description")


def sync_tax(dt, bexio_id, title, rows, disabled, is_default):
    # Creates the template for a bexio id, or brings the one there back to the
    # rows of bexio: re-run changes nothing.
    abbr = frappe.db.get_value("Company", COMPANY, "abbr")
    name = frappe.db.get_value(dt, {"bexio_id": bexio_id}, "name")
    fields = {"title": title, "disabled": disabled}
    if dt != "Item Tax Template":
        fields["is_default"] = is_default
    if not name:
        doc = frappe.get_doc({"doctype": dt, "company": COMPANY, "bexio_id": bexio_id, "taxes": rows, **fields})
        doc.insert()
        return "created"
    doc = frappe.get_doc(dt, name)
    changed = False
    for k, v in fields.items():
        if doc.get(k) != v:
            doc.set(k, v)
            changed = True
    key = row_key(dt)
    if [tuple(r.get(k) for k in key) for r in rows] != [tuple(getattr(r, k) for k in key) for r in doc.taxes]:
        doc.set("taxes", rows)
        changed = True
    if changed:
        doc.save()
    if doc.name != f"{title} - {abbr}":
        frappe.rename_doc(dt, doc.name, f"{title} - {abbr}", force=True)
        return "renamed"
    return "updated" if changed else "unchanged"


def vat():
    # bexio's codes replace ERPNext's own templates (Benchi). fields() first:
    # the templates carry bexio_id, which is their key.
    fields()
    # Item Tax rows only take accounts of type Tax; the KMU template leaves
    # these empty (as coa does for 2200)
    for number in ("1172", "1173", "1174", "2202", "2203"):
        name = account(number)
        if frappe.db.get_value("Account", name, "account_type") != "Tax":
            frappe.db.set_value("Account", name, "account_type", "Tax")
    tally = {}
    for kind, bexio_id, code, name, rate, valid, active, account_no, deduct_no in BEXIO_TAXES:
        title = tax_title(code, rate, name)
        desc = f"bexio {bexio_id}" + (f", gültig {valid}" if valid else "")
        # every code gets an Item Tax Template; sales and purchase templates by kind
        dts = ["Sales Taxes and Charges Template"] if kind == "S" else ["Purchase Taxes and Charges Template"]
        dts.append("Item Tax Template")
        for dt in dts:
            rows = tax_rows(dt, kind, account_no, deduct_no, rate)
            for row in rows if dt != "Item Tax Template" else []:
                row["description"] = desc
            is_default = int(bexio_id in (DEFAULT_SALES, DEFAULT_PURCHASE) and dt != "Item Tax Template")
            result = sync_tax(dt, str(bexio_id), title, rows, 0 if active else 1, is_default)
            tally[(dt, result)] = tally.get((dt, result), 0) + 1
    frappe.db.commit()
    for (dt, result), n in sorted(tally.items()):
        say(f"vat: {dt} {result} {n}")
    retire()
    for dt in TAX_DOCTYPES:
        say(f"vat: {frappe.db.count(dt, {'company': COMPANY})} {dt}")


def retire():
    # ERPNext's own templates have no bexio_id. delete_doc refuses a template a
    # document or item still links to, so nothing is removed under a reference.
    for dt in TAX_DOCTYPES:
        for name in frappe.get_all(dt, {"company": COMPANY}, pluck="name"):
            if frappe.db.get_value(dt, name, "bexio_id"):
                continue
            frappe.delete_doc(dt, name)
            say(f"vat: deleted {dt} {name}")
    frappe.db.commit()


def vat_differences(taxes, numbers, erp):
    # taxes: bexio's /3.0/taxes rows; numbers: bexio account id -> account number;
    # erp: bexio id -> (code, rate, account number, disabled) of the ERPNext template.
    out = []
    seen = set()
    for t in taxes:
        bid = str(t["id"])
        seen.add(bid)
        want = (t["code"], float(t["value"]), numbers.get(str(t.get("account_id"))), not t["is_active"])
        if bid not in erp:
            out.append(f"missing: {bid} {want[0]}")
            continue
        got = erp[bid]
        for label, a, b in zip(("code", "rate", "account", "disabled"), want, got):
            if a != b:
                out.append(f"{bid} {want[0]}: {label} bexio {a!r}, erpnext {b!r}")
    for bid in sorted(set(erp) - seen):
        out.append(f"extra: {bid} {erp[bid][0]} is not in bexio")
    return out


def vat_check():
    # Runs in the container, which cannot see the export: swiss-setup.sh puts
    # taxes.json and accounts.json in TAXES_JSON and ACCOUNTS_JSON.
    taxes = json.loads(os.environ["TAXES_JSON"])
    numbers = {str(a["id"]): str(a["account_no"]) for a in json.loads(os.environ["ACCOUNTS_JSON"])}
    erp = {}
    for dt in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"):
        for name in frappe.get_all(dt, {"company": COMPANY, "bexio_id": ["is", "set"]}, pluck="name"):
            doc = frappe.get_doc(dt, name)
            acc = frappe.db.get_value("Account", doc.taxes[0].account_head, "account_number")
            erp[doc.bexio_id] = (name.split()[0], float(doc.taxes[0].rate), acc, bool(doc.disabled))
    diffs = vat_differences(taxes, numbers, erp)
    for d in diffs:
        say(d)
    say(f"vat --check: {len(diffs)} differences")
    if diffs:
        sys.exit(1)


def fiscal():
    for year in FISCAL_YEARS:
        if frappe.db.exists("Fiscal Year", str(year)):
            doc = frappe.get_doc("Fiscal Year", str(year))
        else:
            doc = frappe.get_doc({
                "doctype": "Fiscal Year", "year": str(year),
                "year_start_date": f"{year}-01-01", "year_end_date": f"{year}-12-31",
            })
        if COMPANY not in [c.company for c in doc.companies]:
            doc.append("companies", {"company": COMPANY})
            doc.save() if doc.get("creation") else doc.insert()
    frappe.db.commit()
    say(f"fiscal: {frappe.db.count('Fiscal Year')} fiscal years")


def fields():
    for dt, after in BEXIO_DOCTYPES:
        if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": "bexio_id"}):
            continue
        if not frappe.get_meta(dt).has_field(after):
            after = None
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": dt,
            "fieldname": "bexio_id",
            "label": "bexio ID",
            "fieldtype": "Data",
            "unique": 1,
            "read_only": 1,
            "in_standard_filter": 1,
            # a copied document must not inherit the id of the original
            "no_copy": 1,
            "insert_after": after,
        }).insert()
    frappe.db.commit()
    say(f"fields: bexio_id on {frappe.db.count('Custom Field', {'fieldname': 'bexio_id'})} doctypes")


def currencies():
    # The API user may not write Currency, so this lives here, not in the importer.
    for code, step in CURRENCIES.items():
        doc = frappe.get_doc("Currency", code)
        want = {"enabled": 1}
        if code == "CHF":
            want["smallest_currency_fraction_value"] = step
        if all(doc.get(k) == v for k, v in want.items()):
            continue
        doc.update(want)
        doc.save()
    frappe.db.commit()
    say(f"currencies: {frappe.db.count('Currency', {'enabled': 1})} enabled")


# Public BIC of the banks the company pays from, by the Bank's name in ERPNext. Bank data,
# not company data; a bank not listed here keeps whatever its Bank form says.
BANK_BICS = {"UBS Switzerland AG": "UBSWCHZH80A"}


def payment_fixes(bank_accounts, gl_accounts, banks):
    # The changes the payments step makes, from plain rows so the rule is testable offline.
    # erpnextswiss reads the IBAN and BIC of a pay-from account from its GL Account, not from
    # the Bank Account. Only empty fields are set, so a value entered by hand is never changed.
    # bank_accounts: [{name, account, iban, bank}], gl_accounts: {account: {iban, bic}},
    # banks: {bank: swift_number}. Returns [(doctype, name, field, value)].
    fixes = {}
    for ba in bank_accounts:
        if not ba["iban"] or not ba["account"]:
            continue
        gl = gl_accounts.get(ba["account"]) or {}
        if not gl.get("iban"):
            fixes.setdefault(("Account", ba["account"], "iban"), ba["iban"])
        bic = banks.get(ba["bank"]) or BANK_BICS.get(ba["bank"])
        if not bic:
            continue
        if not banks.get(ba["bank"]):
            fixes.setdefault(("Bank", ba["bank"], "swift_number"), bic)
        if not gl.get("bic"):
            fixes.setdefault(("Account", ba["account"], "bic"), bic)
    return [(dt, name, field, value) for (dt, name, field), value in fixes.items()]


def payments():
    # Payment runs (bi_finance/payment_run.py). validate_xml stays 0: the app checks each
    # file against the Swiss schema after fixing its country code (see the app). Unidecode
    # turns the names into the character set the bank takes.
    s = frappe.get_single("ERPNextSwiss Settings")
    want = {"validate_xml": 0, "use_unidecode": 1, "xml_version": "09", "banking_region": "CH"}
    changed = [f for f, v in want.items() if s.get(f) != v]
    for f in changed:
        say(f"payments: ERPNextSwiss Settings {f} {s.get(f)} -> {want[f]}")
        s.set(f, want[f])
    if changed:
        s.save()
    else:
        say("payments: ERPNextSwiss Settings already set")

    bank_accounts = frappe.get_all("Bank Account", filters={"company": COMPANY}, fields=["name", "account", "iban", "bank"])
    gl_accounts = {ba.account: frappe.db.get_value("Account", ba.account, ["iban", "bic"], as_dict=True)
                   for ba in bank_accounts if ba.account}
    banks = {b.name: b.swift_number for b in frappe.get_all("Bank", fields=["name", "swift_number"])}
    for dt, name, field, value in payment_fixes(bank_accounts, gl_accounts, banks):
        doc = frappe.get_doc(dt, name)
        doc.set(field, value)
        doc.save()
        say(f"payments: {dt} {name}: {field} set")
    if not bank_accounts:
        say("payments: no bank account of the company")

    account = frappe.db.get_value("Account", {"account_name": "UBS Kontokorrent", "company": COMPANY}, ["name", "iban", "bic"], as_dict=True)
    if not account:
        say("payments: no account UBS Kontokorrent for the company")
    elif not account.iban or not account.bic:
        say(f"payments: {account.name} has no IBAN or no BIC; a bank file cannot be made until both are set")
    frappe.db.commit()


# HR (HR 5, erp-mvr9). The roles HRMS v16.50.0 installs: the role profile "HR"
# holds HR User, HR Manager, Leave Approver and Expense Approver (hrms/setup.py),
# and the custom role "Employee Self Service" gates the self-service portal. It
# ships no Payroll Manager: the payroll doctypes (Salary Slip, Payroll Entry)
# are the HR Manager's. That role is granted only when the site has it; the
# step never creates a role.
HR_ROLES = ["HR Manager", "HR User", "Payroll Manager", "Leave Approver"]
# Payroll Settings (single), Benchi 2026-10-11: payroll works on Leave, not
# attendance; holidays count in the working days; no slip is e-mailed —
# Salary Slip.on_submit mails the slip to the employee when this check is on.
PAYROLL_SETTINGS_WANT = {"payroll_based_on": "Leave", "include_holidays_in_total_working_days": 1,
                         "email_salary_slip_to_employee": 0}
# HR Settings (single): v16.50.0 has no switch for employee self-service or for
# onboarding e-mails. Self-service is off when no enabled user holds the
# Employee Self Service role and no Employee carries a user; onboarding e-mails
# go only from Employee Onboarding documents, of which the site has none (the
# importers write Employees directly). `hr --check` prints the counts.
HR_SETTINGS_WANT = {}
# The roles of an enabled user that make it an HR or payroll user in `hr --check`.
HR_CHECK_ROLES = ("HR Manager", "HR User", "Leave Approver", "Expense Approver",
                  "Payroll Manager", "Employee Self Service")


def hr_grants(wanted, site_roles, user_roles):
    """wanted: roles the goal names; site_roles: Role names on the site;
    user_roles: the user's roles. Returns (roles to add, roles the site has
    no Role document for). A role without a Role document is never added."""
    have_site, have_user = set(site_roles), set(user_roles)
    return ([r for r in wanted if r in have_site and r not in have_user],
            [r for r in wanted if r not in have_site])


def single_fixes(want, current, offered):
    """want: {field: value}; current: {field: value}; offered: the fieldnames
    the single has. Returns ((field, old, new) to set, fields not offered)."""
    changes, not_offered = [], []
    for f, v in want.items():
        if f not in offered:
            not_offered.append(f)
        elif current.get(f) != v:
            changes.append((f, current.get(f), v))
    return changes, not_offered


def hr():
    # Benchi's user (HR_USER in the environment, never in this repo) gets the HR
    # roles; Payroll Settings and HR Settings hold the values above. Read-only
    # counterpart: hr_check().
    user_name = os.environ.get("HR_USER", "").strip()
    if not user_name:
        # Like host: no argument runs every step, and a step that needs a value from the
        # environment skips instead of ending the run.
        say("hr: HR_USER not set; skipped (the user stays out of this repo; pass HR_USER=<user>)")
        return
    if not frappe.db.exists("User", user_name):
        sys.exit(f"hr: no user {user_name}")
    if not frappe.db.get_value("User", user_name, "enabled"):
        sys.exit(f"hr: user {user_name} is not enabled")

    site_roles = frappe.get_all("Role", pluck="name")
    user = frappe.get_doc("User", user_name)
    to_add, not_offered = hr_grants(HR_ROLES, site_roles, [r.role for r in user.roles])
    for role in to_add:
        user.append("roles", {"role": role})
        say(f"hr: {user_name}: role {role} added")
    for role in not_offered:
        say(f"hr: the site has no role {role}; HRMS v16.50.0 installs none and none is created here (payroll sits on HR Manager)")
    if to_add:
        user.save()
    # read-back of every wanted role
    have = {r.role for r in frappe.get_doc("User", user_name).roles}
    for role in HR_ROLES:
        if role in site_roles:
            say(f"hr: {user_name} {role}: {'yes' if role in have else 'NO'}")

    for single, want in (("Payroll Settings", PAYROLL_SETTINGS_WANT), ("HR Settings", HR_SETTINGS_WANT)):
        offered = {f.fieldname for f in frappe.get_meta(single).fields if f.fieldname}
        doc = frappe.get_doc(single)
        changes, missing = single_fixes(want, {f: doc.get(f) for f in want}, offered)
        for f, old, v in changes:
            doc.set(f, v)
            say(f"hr: {single} {f}: {old!r} -> {v!r}")
        if changes:
            doc.save()
        # read-back of every wanted value the single offers
        fresh = frappe.get_doc(single)
        for f in want:
            if f in offered:
                say(f"hr: {single} {f} = {fresh.get(f)!r}")
        for f in missing:
            say(f"hr: {single} has no field {f}; not set")
    say("hr: HRMS v16.50.0 has no rounding setting; CHF rounds to 0.05 by the Currency smallest fraction (currencies step)")
    say("hr: HRMS v16.50.0 has no switch for employee self-service or onboarding e-mails; hr --check reports the counts")
    frappe.db.commit()


def hr_check():
    # Read-only. User names are not company data, but notes keep counts only.
    enabled = set(frappe.get_all("User", filters={"enabled": 1}, pluck="name"))
    holders = [(r.parent, r.role) for r in frappe.get_all(
        "Has Role", filters={"parenttype": "User", "role": ["in", list(HR_CHECK_ROLES)]},
        fields=["parent", "role"]) if r.parent in enabled]
    for user_with_role, role in sorted(holders):
        say(f"hr --check: {user_with_role}: {role}")
    say(f"hr --check: {len({u for u, _ in holders})} enabled users hold HR or payroll roles")
    for single, want in (("Payroll Settings", PAYROLL_SETTINGS_WANT), ("HR Settings", HR_SETTINGS_WANT)):
        offered = {f.fieldname for f in frappe.get_meta(single).fields if f.fieldname}
        for f, v in want.items():
            if f in offered:
                got = frappe.db.get_single_value(single, f)
                say(f"hr --check: {single} {f} = {got!r}" + ("" if got == v else f", want {v!r}"))
    say("hr --check: HRMS v16.50.0 has no rounding setting; CHF rounds to 0.05 by the Currency smallest fraction")
    say(f"hr --check: {frappe.db.count('Employee', {'user_id': ['is', 'set']})} employees carry a user")
    say(f"hr --check: {frappe.db.count('Employee Onboarding')} Employee Onboarding documents")
    user_name = os.environ.get("HR_USER", "").strip()
    if not user_name:
        say("hr --check: HR_USER not set; the user's state is not compared")
        return
    site_roles = frappe.get_all("Role", pluck="name")
    to_add, not_offered = hr_grants(HR_ROLES, site_roles,
                                    [r.role for r in frappe.get_doc("User", user_name).roles])
    wrong = [f"role {r} missing" for r in to_add]
    for single, want in (("Payroll Settings", PAYROLL_SETTINGS_WANT), ("HR Settings", HR_SETTINGS_WANT)):
        offered = {f.fieldname for f in frappe.get_meta(single).fields if f.fieldname}
        doc = frappe.get_doc(single)
        fixes, _ = single_fixes(want, {f: doc.get(f) for f in want}, offered)
        wrong += [f"{single} {f} = {old!r}, want {v!r}" for f, old, v in fixes]
    for w in wrong:
        say(f"hr --check: {user_name}: {w}")
    if wrong:
        sys.exit(f"hr --check: {len(wrong)} differences")
    say(f"hr --check: {user_name} holds the offered HR roles and the settings hold the wanted values")


# The print format is a Jinja file of the bi_finance app (finance/apps/bi_finance),
# which the image installs. The app's qrbill.py draws the QR code.
PRINT_FORMAT = "BI Sales Invoice QR"
QRBILL_HTML = "/home/frappe/frappe-bench/apps/bi_finance/bi_finance/bi_sales_invoice_qr.html"

# The payslip format is a Jinja file of the bi_payroll app (finance/apps/bi_payroll), whose rows
# come from bi_payroll/payslip.py through the jinja method payslip_data (hooks.py).
LOHNABRECHNUNG = "Lohnabrechnung"
PAYSLIP_HTML = "/home/frappe/frappe-bench/apps/bi_payroll/bi_payroll/lohnabrechnung.html"


def host():
    # wkhtmltopdf runs in the backend container and fetches /assets from the site's host_name.
    # Unset, that is frontend port 80, which nothing listens on (nginx is on 8080), so every PDF
    # fails. The URL names this machine, so it comes from HOST_NAME, never from this file.
    url = os.environ.get("HOST_NAME", "").strip()
    if not url:
        say("host: HOST_NAME not set; skipped")
        return
    path = frappe.get_site_path("site_config.json")
    with open(path, encoding="utf-8") as f:
        conf = json.load(f)
    if conf.get("host_name") == url:
        say("host: host_name in place")
        return
    conf["host_name"] = url
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=1, sort_keys=True)
    say("host: host_name set")


def qrbill():
    # The slip format; qrbill_default() sets it as the default print format of Sales Invoice.
    with open(QRBILL_HTML, encoding="utf-8") as f:
        html = f.read()
    if frappe.db.exists("Print Format", PRINT_FORMAT):
        doc = frappe.get_doc("Print Format", PRINT_FORMAT)
        if doc.html == html and not doc.disabled and doc.margin_bottom == 1:
            say("qrbill: print format in place")
        else:
            doc.html = html
            doc.disabled = 0
            doc.margin_bottom = 1
            doc.save()
            say(f"qrbill: print format {PRINT_FORMAT} updated")
    else:
        frappe.get_doc({
            "doctype": "Print Format",
            "name": PRINT_FORMAT,
            "doc_type": "Sales Invoice",
            "module": "Accounts",
            "custom_format": 1,
            "print_format_type": "Jinja",
            "standard": "No",
            "disabled": 0,
            # the payment part sits at the foot of the sheet: 1 mm bottom margin (0 falls back to the 15 mm default)
            "margin_bottom": 1,
            "html": html,
        }).insert()
        say(f"qrbill: print format {PRINT_FORMAT} created")
    frappe.db.commit()
    qrbill_default()


def qrbill_default():
    # The default print format of Sales Invoice (Benchi: 'default now', 2026-10-11), as a Property
    # Setter so a migrate keeps it; the fixtures of bi_finance hold the same row.
    filters = {"doc_type": "Sales Invoice", "doctype_or_field": "DocType", "property": "default_print_format"}
    if frappe.db.get_value("Property Setter", filters, "value") == PRINT_FORMAT:
        say(f"qrbill: default print format of Sales Invoice is {PRINT_FORMAT} already")
        return
    set_doctype_property("Sales Invoice", "default_print_format", PRINT_FORMAT, "Data")
    frappe.clear_cache(doctype="Sales Invoice")
    frappe.db.commit()
    say(f"qrbill: default print format of Sales Invoice set to {PRINT_FORMAT}")


def payslip_changes(doc, html):
    # The fields of an existing Lohnabrechnung Print Format that differ from the file: {} in place.
    want = {"html": html, "disabled": 0}
    return {field: value for field, value in want.items() if doc.get(field) != value}


def payslip():
    # The payslip format; payslip_default() sets it as the default print format of Salary Slip.
    with open(PAYSLIP_HTML, encoding="utf-8") as f:
        html = f.read()
    if frappe.db.exists("Print Format", LOHNABRECHNUNG):
        doc = frappe.get_doc("Print Format", LOHNABRECHNUNG)
        changes = payslip_changes(doc, html)
        if not changes:
            say("payslip: print format in place")
        else:
            for field, value in changes.items():
                doc.set(field, value)
            doc.save()
            say(f"payslip: print format {LOHNABRECHNUNG} updated ({', '.join(changes)})")
    else:
        frappe.get_doc({
            "doctype": "Print Format",
            "name": LOHNABRECHNUNG,
            "doc_type": "Salary Slip",
            "module": "HR",
            "custom_format": 1,
            "print_format_type": "Jinja",
            "standard": "No",
            "disabled": 0,
            "html": html,
        }).insert()
        say(f"payslip: print format {LOHNABRECHNUNG} created")
    frappe.db.commit()
    payslip_default()


def payslip_default():
    # The default print format of Salary Slip, always (the bead's decision: Benchi asked for the
    # HRMS integration), as a Property Setter so a migrate keeps it. The Print Format itself is
    # not a fixture: the setup step installs it from the app's file, as qrbill does.
    filters = {"doc_type": "Salary Slip", "doctype_or_field": "DocType", "property": "default_print_format"}
    if frappe.db.get_value("Property Setter", filters, "value") == LOHNABRECHNUNG:
        say(f"payslip: default print format of Salary Slip is {LOHNABRECHNUNG} already")
        return
    set_doctype_property("Salary Slip", "default_print_format", LOHNABRECHNUNG, "Data")
    frappe.clear_cache(doctype="Salary Slip")
    frappe.db.commit()
    say(f"payslip: default print format of Salary Slip set to {LOHNABRECHNUNG}")


# The Form 11 mapping of erpnextswiss's Salary Certificate Settings (a single doctype): the form field each table
# fills, and the abbrs of the Salary Components it sums (bi_payroll's fixtures). Dynamic configuration reads the
# components of each table; the static one reads the abbrs too, but has no table for line 12. Lines 2 to 7, 10.2 and
# 13 to 14 are not in payroll (finance/docs/hrms.md, Lohnausweis map), line 15 stays free text.
LOHNAUSWEIS = {
    "salary": ["B"],                        # 1 Lohn
    "deduction_ahv": ["AHV", "ALV", "NBUV"],  # 9 AHV/IV/EO, ALV, NBU
    "deduction_pension": ["PK"],            # 10.1 BVG
    "source_tax_deduction": ["QST"],        # 12 Quellensteuer
}


def lohnausweis_components(components, mapping=LOHNAUSWEIS):
    """components: abbr -> the name of its Salary Component. The component names per Settings table; an abbr with no
    component raises KeyError."""
    return {field: [components[abbr] for abbr in abbrs] for field, abbrs in mapping.items()}


def lohnausweis_changes(dynamic_config, current, wanted):
    """current: field -> the component names its table holds now. Returns whether dynamic configuration must be switched
    on, and the tables that differ from wanted. A second run finds nothing to change."""
    return not dynamic_config, [field for field in wanted if current.get(field, []) != wanted[field]]


def lohnausweis():
    # The company's address on the form is the primary address of the Company record (erpnextswiss's print format reads
    # it through the employee's company): nothing about the company is written here, only checked.
    components = {}
    for abbr in sorted({abbr for abbrs in LOHNAUSWEIS.values() for abbr in abbrs}):
        name = frappe.db.get_value("Salary Component", {"salary_component_abbr": abbr})
        if not name:
            say(f"lohnausweis: no Salary Component with abbr {abbr}; the bi_payroll fixtures are not installed")
            return
        components[abbr] = name
    wanted = lohnausweis_components(components)
    settings = frappe.get_single("Salary Certificate Settings")
    current = {field: [row.component for row in settings.get(field) or []] for field in wanted}
    turn_on, fields = lohnausweis_changes(settings.dynamic_config, current, wanted)
    if turn_on or fields:
        settings.dynamic_config = 1
        for field in fields:
            settings.set(field, [{"component": name} for name in wanted[field]])
        settings.save()
        frappe.db.commit()
        say(f"lohnausweis: Salary Certificate Settings set, tables {', '.join(fields) or 'none'}")
    else:
        say("lohnausweis: Salary Certificate Settings in place")
    primary = frappe.db.sql(
        """SELECT a.name FROM `tabAddress` a INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address' AND dl.link_doctype = 'Company' AND dl.link_name = %s AND a.is_primary_address = 1""",
        COMPANY)
    if not primary:
        say(f"lohnausweis: {COMPANY} has no primary address; the Form 11 address will be empty")


def banks():
    # Bank needs System Manager, which the API user lacks. The names are company
    # data, so they come from the BANKS environment variable (one per line),
    # never from this file: import_master.py --print-banks lists them.
    for name in filter(None, (n.strip() for n in os.environ.get("BANKS", "").split("\n"))):
        if not frappe.db.exists("Bank", name):
            frappe.get_doc({"doctype": "Bank", "bank_name": name}).insert()
    frappe.db.commit()
    say(f"banks: {frappe.db.count('Bank')} banks")


def gebuev():
    # 1. Accounts Settings. Cancelling with the immutable ledger posts reversal
    # entries and leaves the original GL rows as they were; without it, the
    # original rows get is_cancelled=1 in place.
    s = frappe.get_single("Accounts Settings")
    want = {"enable_immutable_ledger": 1, "delete_linked_ledger_entries": 0}
    changed = [f for f, v in want.items() if s.get(f) != v]
    for f in changed:
        say(f"gebuev: Accounts Settings {f} {s.get(f)} -> {want[f]}")
        s.set(f, want[f])
    if changed:
        s.save()
    else:
        say("gebuev: Accounts Settings already set")

    # 2. Delete right: only GEBUEV_DELETE_ROLE keeps it on these doctypes.
    # frappe refuses to delete a submitted document for everyone, so this
    # covers drafts and cancelled documents.
    for dt in GEBUEV_TRACKED:
        perms = [(p.role, p.permlevel, p.delete) for p in frappe.get_meta(dt).permissions]
        for role, level in delete_revokes(perms, keep=[GEBUEV_DELETE_ROLE]):
            frappe.permissions.update_permission_property(dt, role, level, "delete", 0)
            say(f"gebuev: {dt}: delete revoked from {role} (permlevel {level})")
        frappe.clear_cache(doctype=dt)
    say("gebuev: delete rights checked on " + str(len(GEBUEV_TRACKED)) + " doctypes")

    # 3. Track Changes (Version) on the same doctypes. A Property Setter, so a
    # migrate does not reset it to the doctype's JSON.
    for dt in GEBUEV_TRACKED:
        if frappe.db.get_value("DocType", dt, "track_changes"):
            continue
        set_doctype_property(dt, "track_changes", 1, "Check")
        say(f"gebuev: {dt}: track changes on")
    frappe.clear_cache()
    say("gebuev: track changes checked on " + str(len(GEBUEV_TRACKED)) + " doctypes")
    frappe.db.commit()


def freeze(date, apply):
    # Accounts frozen till a date: no posting dated on or before it. The role
    # that may still post there is set by hand, not here. Dry run unless
    # --apply. The history import comes first, so this is not run before it.
    company = frappe.get_doc("Company", COMPANY)
    current = company.accounts_frozen_till_date
    if current and str(current) > date:
        sys.exit(f"STOP: accounts are frozen till {current}; a freeze is not moved back to {date}")
    gl = frappe.db.count("GL Entry", {"company": COMPANY, "posting_date": ["<=", date], "is_cancelled": 0})
    say(f"freeze: accounts frozen till {current or '(none)'} -> {date}")
    say(f"freeze: {gl} GL entries dated on or before {date}")
    say(f"freeze: role allowed to post into frozen periods: {company.role_allowed_for_frozen_entries or '(none)'}")
    if not apply:
        say("freeze: dry run, nothing changed; add --apply to set it")
        return
    frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", date)
    frappe.db.commit()
    say(f"freeze: set, accounts frozen till {date}")


def payroll_accounts():
    # HRMS's accrual Journal Entry credits the net wages to the Company's default payroll payable account; bexio's is
    # 1091 Lohndurchlaufkonto (bi_payroll.swiss_rates). Set here, not in the fixtures: the Company is not a fixture.
    from bi_payroll.swiss_rates import PAYROLL_PAYABLE_ACCOUNT

    payable = account(PAYROLL_PAYABLE_ACCOUNT)
    if frappe.db.get_value("Company", COMPANY, "default_payroll_payable_account") == payable:
        say("payroll_accounts: default payroll payable already set")
        return
    frappe.db.set_value("Company", COMPANY, "default_payroll_payable_account", payable)
    frappe.db.commit()
    say(f"payroll_accounts: default payroll payable set to {PAYROLL_PAYABLE_ACCOUNT}")


STEPS = {"coa": coa, "vat": vat, "fiscal": fiscal, "fields": fields, "currencies": currencies, "banks": banks,
         "gebuev": gebuev, "qrbill": qrbill, "payslip": payslip, "host": host, "payments": payments, "hr": hr,
         "payroll_accounts": payroll_accounts, "lohnausweis": lohnausweis}

if __name__ == "__main__":
    args = sys.argv[1:]
    check = "--check" in args
    steps = [a for a in args if a != "--check"] or list(STEPS)
    frappe.init(site="frontend")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        if args[:1] == ["freeze"]:
            freeze(*parse_freeze(args[1:]))
        elif check:
            if steps == ["hr"]:
                hr_check()
            else:
                vat_check()
        else:
            for s in steps:
                STEPS[s]()
    finally:
        frappe.destroy()
