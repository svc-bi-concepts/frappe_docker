"""The VAT Declaration of the effective method on erpnextswiss's form: the payment-basis fill and the XML download.

Two changes to erpnextswiss's doctype (hooks.py, override_doctype_class):
- fill_payment_basis: a draft for one period with the figures of the payment basis, the same rows and rules as
  mwst_report.py --basis payment (mwst_payment.py). erpnextswiss's own fetch (the form's Get values button) reads the
  invoice basis from its views; a payment-basis declaration must not be filled that way.
- create_transfer_file: the XML as ESTV takes it, eCH-0217 version 2.0.0 (estv_vat_xml.py). erpnextswiss renders
  version 1, which the ESTV portal no longer takes.
"""

import datetime
from decimal import Decimal

import frappe
from erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration import VATDeclaration as SwissVATDeclaration
from frappe import _
from frappe.utils import getdate

from bi_finance import __version__, estv_vat_xml, mwst_payment


class VATDeclaration(SwissVATDeclaration):
    def create_transfer_file(self):
        # the flat rate method has its own form rows, which this file does not make; erpnextswiss reads anything else as effective
        if self.vat_type == "flat":
            frappe.throw(_("The flat rate method has no XML here. Set the VAT type to effective."))
        try:
            uid = estv_vat_xml.uid_of(frappe.get_value("Company", self.company, "tax_id"))
        except ValueError:
            frappe.throw(_("Tax ID/UID missing or invalid. Please configure for company {0}.").format(self.company))
        figures = {ziffer: getattr(self, field) or 0 for ziffer, field in mwst_payment.FIELD_BY_ZIFFER.items()}
        content = estv_vat_xml.declaration_xml(
            figures, uid=uid, company=self.company, title=self.title,
            start_date=getdate(self.start_date), end_date=getdate(self.end_date),
            generated=datetime.datetime.now(datetime.timezone.utc), product_version=__version__)
        return {"content": content}


@frappe.whitelist(methods=["POST"])
def fill_payment_basis(company, period, dry_run=0):
    """The draft VAT Declaration of one period on the payment basis: created, or its draft updated.

    period: '2026Q3' or '2026H1', as mwst_report.py takes it. A declaration of the period that is submitted stops the
    fill; it is never changed. dry_run: compute the fields and return them, write nothing; a write returns the draft's name with them. The fill sets Ziffer 200,
    the rate rows, the Bezugsteuer, the Vorsteuer and the Umsatz deductions 220, 221, 225, 230 and 235 from the sales
    templates (mwst_payment.py); 205 and 280 stay as they are, and 289 and 299 the form computes itself on save
    (erpnextswiss's vat_declaration.js). A quarter whose 200 less the deductions is off the rate bases beyond
    rounding is refused, so a draft the ESTV would not take is never written."""
    frappe.has_permission("VAT Declaration", "write", throw=True)
    start, end = mwst_payment.period_bounds(period)
    try:
        fields = mwst_payment.declaration_fields(_payment_totals(company, start, end))
    except ValueError as err:
        frappe.throw(_("The declaration of {0} is not filled: {1}").format(period, err))
    shown = {name: str(value) for name, value in fields.items()}
    if int(dry_run):
        return {"fields": shown}
    # a dry run shows the figures with any Tax ID; a draft needs the company's UID in the pattern, no guess at it
    try:
        estv_vat_xml.uid_of(frappe.get_value("Company", company, "tax_id"))
    except ValueError:
        frappe.throw(_("Tax ID/UID missing or invalid. Please configure for company {0}.").format(company))

    drafts = frappe.get_all("VAT Declaration", filters={"company": company, "start_date": start, "end_date": end,
                                                        "docstatus": ["in", [0, 1]]},
                            fields=["name", "docstatus"])
    if any(d.docstatus == 1 for d in drafts):
        frappe.throw(_("A submitted VAT Declaration exists for {0}; it is not changed.").format(period))
    if len(drafts) > 1:
        frappe.throw(_("{0} drafts exist for {1}; keep one.").format(len(drafts), period))
    if drafts:
        doc = frappe.get_doc("VAT Declaration", drafts[0].name)
    else:
        doc = frappe.new_doc("VAT Declaration")
        doc.update({"title": "MWST {}".format(period), "company": company, "start_date": start, "end_date": end})
    doc.vat_type = "effective"
    doc.update({name: float(value) for name, value in fields.items()})
    doc.save()
    # the desk routes to this draft: the new form it was opened from is not saved
    return {"name": doc.name, "fields": shown}


def _payment_totals(company, start, end):
    """The payment basis of the period, read from frappe.db: the rows mwst_report.py reads over the REST API."""
    templates = {}
    for doctype in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"):
        for t in frappe.get_all(doctype, filters={"bexio_id": ["is", "set"]}, fields=["name", "bexio_id"]):
            templates[t.name] = str(t.bexio_id)

    payments, invoices = [], {}
    for p in frappe.get_all("Payment Entry", filters={"company": company, "docstatus": 1,
                                                      "posting_date": ["between", [start, end]]},
                            fields=["name", "posting_date"]):
        references = [{"reference_doctype": r.reference_doctype, "reference_name": r.reference_name,
                       "allocated_amount": r.allocated_amount}
                      for r in frappe.get_doc("Payment Entry", p.name).references]
        for ref in references:
            key = (ref["reference_doctype"], ref["reference_name"])
            if ref["reference_doctype"] in mwst_payment.INVOICE_DOCTYPES and key not in invoices:
                invoices[key] = frappe.get_doc(*key).as_dict()
        payments.append({"name": p.name, "posting_date": p.posting_date, "references": references})

    sales, purchases, _unallocated = mwst_payment.payment_documents(payments, invoices)
    totals = mwst_payment.plan(sales, purchases, templates)
    mwst_payment.journal_totals(_journal_lines(company, start, end), totals)
    return totals


def _journal_lines(company, start, end):
    """The GL rows of the period's Journal Entries with their voucher's bexio key, as journal_totals() takes them."""
    numbers = {a.name: a.account_number
               for a in frappe.get_all("Account", filters={"company": company}, fields=["name", "account_number"])}
    keys = {e.name: (e.bexio_id or None)
            for e in frappe.get_all("Journal Entry", filters={"company": company, "docstatus": 1,
                                                              "posting_date": ["between", [start, end]]},
                                    fields=["name", "bexio_id"])}
    lines = []
    for e in frappe.get_all("GL Entry", filters={"company": company, "voucher_type": "Journal Entry",
                                                 "is_cancelled": 0, "posting_date": ["between", [start, end]]},
                            fields=["voucher_no", "posting_date", "account", "debit", "credit"]):
        lines.append({
            "voucher": e.voucher_no,
            "posting_date": e.posting_date,
            "account": numbers.get(e.account),
            "amount": Decimal(str(e.debit)) - Decimal(str(e.credit)),
            "key": keys.get(e.voucher_no),
        })
    return lines
