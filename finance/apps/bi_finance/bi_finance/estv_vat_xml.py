"""The VAT declaration as ESTV's upload file: eCH-0217 version 2.0.0, effective method, received payments.

erpnextswiss renders version 1 (templates/xml/taxes_effective.html). ESTV's FAQ says the XML upload takes
version 2.0.0 only and does not support other versions, so the form gets this file instead. It writes the
declaration's figures by Ziffer, as erpnextswiss's fields hold them: the turnover of each row and the tax of
each input row. The figures are keyed by the Ziffer as a string ('302', '400', ...); a missing one is 0.

Standard library only, so the gate tests it on the host. The VAT Declaration override (vat_declaration.py)
calls it from the form's download. The structure follows ESTV's own example in fixtures/ (test_estv_vat_xml.py).
"""

import datetime
import re
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

ECH_0217 = "http://www.ech.ch/xmlns/eCH-0217/2"
ECH_0058 = "http://www.ech.ch/xmlns/eCH-0058/5"
CENT = Decimal("0.01")

# formOfReporting: 1 vereinbart (invoices by their date), 2 vereinnahmt (payments received). Benchi's method is
# vereinnahmt (D1, 2026-10-10); erpnextswiss always wrote 1 here, which would file the invoice basis.
VEREINNAHMT = 2
# grossOrNet: 1 is netto, ESTV's recommendation; the figures here are the net of the tax.
NETTO = 1
# typeOfSubmission: 1 is the first filing; a correction is 2, which this file does not make yet.
ERSTEINREICHUNG = 1

# The turnover rows of the form, with the rate of each. The rates changed on 2024-01-01: 302, 312 and 342 carry
# the old rates before, 303, 313 and 343 the new ones. 342 is 3.7 %, the old Sondersatz; erpnextswiss's template
# gave it 3.8 % for every period, which is the rate of 343.
SUPPLY_ROWS = [("302", "7.7"), ("303", "8.1"), ("312", "2.5"), ("313", "2.6"), ("342", "3.7"), ("343", "3.8")]

# The totals of turnoverComputation, each with its Ziffer.
TURNOVER_ROWS = [("totalConsideration", "200"), ("suppliesToForeignCountries", "220"), ("suppliesAbroad", "221"),
                 ("transferNotificationProcedure", "225"), ("suppliesExemptFromTax", "230"),
                 ("reductionOfConsideration", "235")]

# The input tax of effectiveReportingMethod, with its Ziffer. 420 is the form's Vorsteuerkürzungen (inputTaxReductions);
# the total of the Vorsteuern is 479, which the file does not carry.
INPUT_TAX_ROWS = [("inputTaxMaterialAndServices", "400"), ("inputTaxInvestments", "405"),
                  ("subsequentInputTaxDeduction", "410"), ("inputTaxCorrections", "415"),
                  ("inputTaxReductions", "420")]

# The acquisition tax (Bezugsteuer): the rate of the period, and the Ziffer that carries it (382 to 2023, 383 from 2024).
ACQUISITION_RATE_CHANGE = datetime.date(2024, 1, 1)


# The company's Tax ID as ERPNext holds it: CHE-123.456.789, with or without the ' MWST' suffix of the VAT registration.
UID_PATTERN = re.compile(r"CHE-\d{3}\.\d{3}\.\d{3}( MWST)?")


def uid_of(tax_id):
    """The nine digits of the company's UID (CHE-123.456.789 MWST): eCH-0108 takes CHE and the number only.

    Any other form is refused, not read: digits in another form may be a number of another kind."""
    if not UID_PATTERN.fullmatch((tax_id or "").strip()):
        raise ValueError("the company's Tax ID is not CHE-123.456.789 (with or without MWST), ESTV needs the UID")
    return re.sub(r"\D", "", tax_id)


def _amount(value):
    return "{:.2f}".format(Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP))


def _el(parent, name, text=None, ns=ECH_0217):
    el = ET.SubElement(parent, "{%s}%s" % (ns, name))
    if text is not None:
        el.text = text
    return el


def declaration_xml(figures, uid, company, title, start_date, end_date, generated,
                    product_version, form_of_reporting=VEREINNAHMT):
    """The eCH-0217 2.0.0 file of one declaration, as text.

    figures: Ziffer (str) -> amount. uid: the nine digits from uid_of(). start_date, end_date: the period's first and
    last day. generated: the time of the file, timezone-aware (written in UTC, as eCH-0217 asks). product_version: the
    version of this app, named in the sending application.
    """
    ET.register_namespace("eCH-0217", ECH_0217)
    ET.register_namespace("eCH-0058", ECH_0058)

    def fig(ziffer):
        return figures.get(ziffer)

    root = ET.Element("{%s}VATDeclaration" % ECH_0217)

    info = _el(root, "generalInformation")
    _el(info, "uid", "CHE" + uid)
    _el(info, "organisationName", company)
    _el(info, "generationTime", generated.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _el(info, "reportingPeriodFrom", start_date.isoformat())
    _el(info, "reportingPeriodTill", end_date.isoformat())
    _el(info, "typeOfSubmission", str(ERSTEINREICHUNG))
    _el(info, "formOfReporting", str(form_of_reporting))
    _el(info, "businessReferenceId", title)
    sending = _el(info, "sendingApplication")
    _el(sending, "manufacturer", "BI Concepts", ECH_0058)
    _el(sending, "product", "bi_finance", ECH_0058)
    _el(sending, "productVersion", product_version, ECH_0058)

    turnover = _el(root, "turnoverComputation")
    for name, ziffer in TURNOVER_ROWS:
        _el(turnover, name, _amount(fig(ziffer)))

    method = _el(root, "effectiveReportingMethod")
    _el(method, "grossOrNet", str(NETTO))
    _el(method, "opted", "0.00")
    for ziffer, rate in SUPPLY_ROWS:
        row = _el(method, "suppliesPerTaxRate")
        _el(row, "taxRate", rate)
        _el(row, "turnover", _amount(fig(ziffer)))
    # Bezugsteuer: its rate is the period's, its base the Ziffer of the period (382 or 383).
    acquisition = _el(method, "acquisitionTax")
    rate_before = start_date < ACQUISITION_RATE_CHANGE
    _el(acquisition, "taxRate", "7.70" if rate_before else "8.10")
    _el(acquisition, "turnover", _amount(fig("382") if rate_before else fig("383")))
    for name, ziffer in INPUT_TAX_ROWS:
        _el(method, name, _amount(fig(ziffer)))

    _el(root, "payableTax", _amount(fig("500")))
    flows = _el(root, "otherFlowsOfFunds")
    _el(flows, "subsidies", _amount(fig("900")))
    _el(flows, "donations", _amount(fig("910")))

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
