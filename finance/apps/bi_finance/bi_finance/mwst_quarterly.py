"""The 'MWST pro Quartal' report's pure half: the ESTV Ziffern per quarter, bexio next to ERPNext, no frappe.

mwst_report.py (on the host) and the VAT Declaration's fill build a period's payment-basis figures with
mwst_payment.py; this module lays them out as the report shows them: one row per form Ziffer, per quarter
the ERPNext figure, bexio's figure where the doctype 'MWST bexio Ziffer' has one, and the difference.
The Ziffern the module does not compute yet (the Umsatz rows 220 to 235, 410, 415, 420, and 510, 900, 910)
stay blank; the report's message says so. The drill-down lists a Ziffer's vouchers from the totals
(mwst_payment.voucher_rows). Pure Python, so the offline tests run on the host.
"""

from decimal import ROUND_HALF_UP, Decimal

from bi_finance import mwst_payment

CENT = Decimal("0.01")
ZERO = Decimal("0")

# The report's rows in the form's order: (Ziffer, label, kind). Kind 'Entgelt' shows the base (the
# vereinnahmte Entgelte), 'Steuer' the tax: the figure the form states for the row — Entgelt for the
# Umsatz and rate rows, Steuer for the Bezugsteuer, the totals and the Vorsteuern (whose base the form
# has none of; mwst_ziffern_compare.py compares them the same way). The labels of 220 to 235 are the
# form's rows erp-vqfg fills; 510, 900 and 910 have no source yet, as the message tells.
ROWS = [
    ("200", "Gesamtbetrag der Entgelte", "Entgelt"),
    ("220", "Steuerfreie Umsätze", "Entgelt"),
    ("221", "Umsätze im Ausland", "Entgelt"),
    ("225", "Übertragungen", "Entgelt"),
    ("230", "Nicht steuerbare Leistungen", "Entgelt"),
    ("235", "Verluste", "Entgelt"),
    ("302", "Normalsatz 7.7 %", "Entgelt"),
    ("303", "Normalsatz 8.1 %", "Entgelt"),
    ("312", "Reduzierter Satz 2.5 %", "Entgelt"),
    ("313", "Reduzierter Satz 2.6 %", "Entgelt"),
    ("342", "Sondersatz 3.7 % (Beherbergung)", "Entgelt"),
    ("343", "Sondersatz 3.8 % (Beherbergung)", "Entgelt"),
    ("382", "Bezugsteuer (bis 2023)", "Steuer"),
    ("383", "Bezugsteuer (ab 2024)", "Steuer"),
    ("399", "Total der geschuldeten Steuer", "Steuer"),
    ("400", "Vorsteuer Material- und Dienstleistungsaufwand", "Steuer"),
    ("405", "Vorsteuer Investitionen und übriger Betriebsaufwand", "Steuer"),
    ("410", "Verminderung der Vorsteuer (offen)", "Steuer"),
    ("415", "Vorsteuerkorrekturen (offen)", "Steuer"),
    ("420", "Vorsteuerkürzungen (offen)", "Steuer"),
    ("479", "Total der Vorsteuern", "Steuer"),
    ("500", "Zu zahlende Steuer (399 − 479)", "Steuer"),
    ("510", "Von der Steuer abzuziehen (offen)", "Steuer"),
    ("900", "Subventionsbeiträge (offen)", "Steuer"),
    ("910", "Spenden (offen)", "Steuer"),
]

OPEN_ROWS = ("220", "221", "225", "230", "235", "410", "415", "420", "510", "900", "910")


def quarters_of(year, quarter=None):
    """The quarters of the year as the periods the module takes ('2026Q1', ...), or the one quarter.
    Raises ValueError for anything else, as the report's filter should say so."""
    year = int(year)
    if quarter:
        part = str(quarter).strip().upper()
        return ["{}{}".format(year, part)] if part in ("Q1", "Q2", "Q3", "Q4") else []
    return ["{}Q{}".format(year, n) for n in (1, 2, 3, 4)]


def figures_of(totals):
    """{Ziffer: [base, tax]} of one period's totals, cent-rounded, with the totals 399 and 479 and
    500 (the payable, the saldo the form_totals computes)."""
    rows, saldo = mwst_payment.form_totals(totals)
    out = {}
    for ziffer, acc in rows.items():
        out[ziffer] = [acc[0].quantize(CENT, rounding=ROUND_HALF_UP), acc[1].quantize(CENT, rounding=ROUND_HALF_UP)]
    out["500"] = [ZERO, saldo.quantize(CENT, rounding=ROUND_HALF_UP)]
    return out


def _figure(acc, kind):
    """The figure of one Ziffer as its kind asks: the base for Entgelt rows, the tax for Steuer rows."""
    if acc is None:
        return None
    return acc[0] if kind == "Entgelt" else acc[1]


def ziffern_rows(quarters, figures_by_quarter, bexio_by_quarter):
    """One report row per form Ziffer: the Ziffer, its label, and per quarter the ERPNext figure, bexio's
    figure (None where the doctype has no row) and the difference (None without bexio's). figures_by_quarter
    maps a quarter to figures_of()'s dict, bexio_by_quarter to {Ziffer: [base, tax]}."""
    rows = []
    for ziffer, label, kind in ROWS:
        row = {"ziffer": ziffer, "label": label, "kind": kind}
        for quarter in quarters:
            erp = _figure(figures_by_quarter.get(quarter, {}).get(ziffer), kind)
            bex = _figure(bexio_by_quarter.get(quarter, {}).get(ziffer), kind)
            # a Ziffer the module did not fill is a true zero (it adds up the whole quarter); bexio without
            # its own row stays blank, so the column says 'no declaration' and not 'nothing declared'
            row["{} erpnext".format(quarter)] = ZERO if erp is None else erp
            row["{} bexio".format(quarter)] = bex
            row["{} differenz".format(quarter)] = None if bex is None else erp - bex
        rows.append(row)
    return rows


def belege_rows(totals, ziffer):
    """The drill-down of one Ziffer: (doctype, name, posting date, base, tax) per voucher, the kind's
    figure being which of the two the caller shows. The Ziffer's own name rides along for the view."""
    return [{"doctype": doctype, "name": name, "posting_date": day,
             "base": base.quantize(CENT, rounding=ROUND_HALF_UP), "tax": tax.quantize(CENT, rounding=ROUND_HALF_UP)}
            for doctype, name, day, base, tax in mwst_payment.voucher_rows(totals, ziffer)]


def message(quarters, with_bexio):
    """The report's basis line: which quarters are shown, on which basis, and what stays open.
    Counts and names, no amounts (the repo is public)."""
    lines = ["Basis: vereinnahmte Entgelte, ESTV Ziffern ({}).".format(", ".join(quarters) if quarters else "kein Quartal")]
    if with_bexio:
        lines.append("bexio: Ziffern aus dem Export, bis 2026Q3; ohne eigene Zeile bleibt die Spalte leer.")
    lines.append("Ohne Figur, bis eine sie rechnet: {}.".format(", ".join(OPEN_ROWS)))
    return " ".join(lines)
