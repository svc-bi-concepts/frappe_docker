"""The VAT Declaration's XML download as ESTV takes it: eCH-0217 version 2.0.0 (estv_vat_xml.py).

erpnextswiss's create_transfer_file renders version 1, which ESTV's portal no longer takes, and writes formOfReporting 1
(invoice basis) whatever the company's method. Only the download is replaced here. The figures stay the form's own
fields, which erpnextswiss's form fills from the invoice basis; the payment basis is not in them yet.
"""

import datetime

import frappe
from erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration import VATDeclaration as SwissVATDeclaration
from frappe import _
from frappe.utils import getdate

from bi_finance import __version__, estv_vat_xml

# The form's fields by Ziffer, as erpnextswiss's create_transfer_file maps them.
FIELD_BY_ZIFFER = {
    "200": "total_revenue",
    "220": "tax_free_services",
    "221": "revenue_abroad",
    "225": "transfers",
    "230": "non_taxable_services",
    "235": "losses",
    "302": "normal_amount",
    "303": "normal_amount_2024",
    "312": "reduced_amount",
    "313": "reduced_amount_2024",
    "342": "lodging_amount",
    "343": "lodging_amount_2024",
    "382": "additional_amount",
    "383": "additional_amount_2024",
    "400": "pretax_material",
    "405": "pretax_investments",
    "410": "missing_pretax",
    "415": "pretax_correction_mixed",
    "420": "pretax_correction_other",
    "500": "payable_tax",
    "900": "grants",
    "910": "donations",
}


class VATDeclaration(SwissVATDeclaration):
    def create_transfer_file(self):
        # the flat rate method has its own form rows, which this file does not make; erpnextswiss reads anything else as effective
        if self.vat_type == "flat":
            frappe.throw(_("The flat rate method has no XML here. Set the VAT type to effective."))
        try:
            uid = estv_vat_xml.uid_of(frappe.get_value("Company", self.company, "tax_id"))
        except ValueError:
            frappe.throw(_("Tax ID/UID missing or invalid. Please configure for company {0}.").format(self.company))
        figures = {ziffer: getattr(self, field) or 0 for ziffer, field in FIELD_BY_ZIFFER.items()}
        content = estv_vat_xml.declaration_xml(
            figures, uid=uid, company=self.company, title=self.title,
            start_date=getdate(self.start_date), end_date=getdate(self.end_date),
            generated=datetime.datetime.now(datetime.timezone.utc), product_version=__version__)
        return {"content": content}
