"""MWST pro Quartal: the ESTV Ziffern of a year (or one quarter) on the payment basis, bexio next to them.

Each quarter's figures come from the same fetch the VAT Declaration's fill uses (_payment_totals, the rows
of mwst_payment.py): the Payment Entries over the invoices they pay and the Journal Entries' VAT. bexio's
column joins the doctype 'MWST bexio Ziffer' by quarter and Ziffer; a quarter without bexio rows shows
blanks there. The difference is ERPNext less bexio. Entgelt rows show the base, Steuer rows the tax, as
the form states them. The layout is mwst_quarterly.py (pure, offline-tested); this file only fetches.

The view 'Belege' lists one Ziffer's vouchers (the Payment Entries with their shares, the Journal
Entries), which the Ziffern view's row click opens.
"""

import frappe
from frappe.utils import flt, getdate, nowdate

from bi_finance import mwst_payment, mwst_quarterly
from bi_finance.vat_declaration import _payment_totals


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    year = filters.year or getdate(nowdate()).year
    quarters = mwst_quarterly.quarters_of(year, filters.get("quarter"))
    if not quarters:
        return _columns([]), [], "Quartal: Q1 bis Q4.", None

    view = filters.get("ansicht") or "Ziffern"
    if view == "Belege":
        return _belege(company, quarters, filters)

    figures, bexio = {}, {}
    for quarter in quarters:
        start, end = mwst_payment.period_bounds(quarter)
        figures[quarter] = mwst_quarterly.figures_of(_payment_totals(company, start, end))
    for row in frappe.get_all("MWST bexio Ziffer", filters=[["quarter", "in", quarters]],
                              fields=["quarter", "ziffer", "base", "tax"]):
        bexio.setdefault(row.quarter, {})[row.ziffer] = [flt(row.base), flt(row.tax)]

    rows = mwst_quarterly.ziffern_rows(quarters, figures, bexio)
    for row in rows:
        row["ziffer"] = str(row["ziffer"])
        for quarter in quarters:
            for what in ("erpnext", "bexio", "differenz"):
                key = "{} {}".format(quarter, what)
                if row[key] is not None:
                    row[key] = flt(row[key])
    message = mwst_quarterly.message(quarters, bool(bexio))
    return _columns(quarters), rows, message, None


def _belege(company, quarters, filters):
    """One Ziffer's vouchers in one quarter: the drill-down the Ziffern view's row click opens."""
    # the filter holds Q1..Q4 and the year; the module's period is 2026Q1
    quarter = quarters[0] if filters.get("quarter") and quarters else None
    ziffer = filters.get("ziffer")
    if not (quarter and ziffer):
        return _columns([]), [], "Belege: ein Quartal und eine Ziffer waehlen (auf eine Zeile der Ziffern-Ansicht klicken).", None
    start, end = mwst_payment.period_bounds(quarter)
    totals = _payment_totals(company, start, end)
    rows = []
    for beleg in mwst_quarterly.belege_rows(totals, str(ziffer).strip()):
        rows.append({
            "ziffer": str(ziffer).strip(),
            "doctype": beleg["doctype"],
            "name": beleg["name"],
            "posting_date": beleg["posting_date"],
            "base": flt(beleg["base"]),
            "tax": flt(beleg["tax"]),
        })
    return (_belege_columns(), rows,
            "Belege von Ziffer {0} in {1} auf der Zahlungsbasis; ihre Anteile summieren sich in die Ziffer.".format(
                str(ziffer).strip(), quarter), None)


def _columns(quarters):
    """The Ziffern view's columns: the Ziffer, its label, and per quarter ERPNext, bexio, difference."""
    out = [
        {"label": "Ziffer", "fieldname": "ziffer", "fieldtype": "Data", "width": 80},
        {"label": "Bezeichnung", "fieldname": "label", "fieldtype": "Data", "width": 340},
    ]
    for quarter in quarters:
        out += [
            {"label": "{} ERPNext".format(quarter), "fieldname": "{} erpnext".format(quarter),
             "fieldtype": "Currency", "width": 130},
            {"label": "{} bexio".format(quarter), "fieldname": "{} bexio".format(quarter),
             "fieldtype": "Currency", "width": 130},
            {"label": "{} Differenz".format(quarter), "fieldname": "{} differenz".format(quarter),
             "fieldtype": "Currency", "width": 130},
        ]
    return out


def _belege_columns():
    return [
        {"label": "Ziffer", "fieldname": "ziffer", "fieldtype": "Data", "width": 80},
        {"label": "Belegtyp", "fieldname": "doctype", "fieldtype": "Data", "width": 130},
        {"label": "Beleg", "fieldname": "name", "fieldtype": "Data", "width": 220},
        {"label": "Datum", "fieldname": "posting_date", "fieldtype": "Date", "width": 110},
        {"label": "Entgelt", "fieldname": "base", "fieldtype": "Currency", "width": 130},
        {"label": "Steuer", "fieldname": "tax", "fieldtype": "Currency", "width": 130},
    ]
