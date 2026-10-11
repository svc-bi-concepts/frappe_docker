"""One of bexio's MWST form figures: the Entgelt and Steuer of one ESTV Ziffer in one quarter.

The rows are reference data the host loader (finance/bexio/load_mwst_bexio.py) upserts from the private
CSV of mwst_bexio.py, through the API user. One row per quarter and Ziffer: the name is
MWST-BEXIO-<quarter>-<ziffer>, so the pair is unique — a second row of the pair cannot exist.
"""

from frappe.model.document import Document


class MWSTbexioZiffer(Document):
    pass
