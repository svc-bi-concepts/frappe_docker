"""The SCOR creditor reference (ISO 11649) of a Sales Invoice: built from its number, read back by the matcher.

Pure Python, no site: qrbill prints it on the slip and in the QR text, bank_match finds the invoice a camt or Wise line
names by it. The same function on both sides, so the two cannot drift apart.
"""

import re


def creditor_reference(invoice_name):
    """SCOR creditor reference (ISO 11649) from the invoice number: 'RF' + check digits + the number.

    The number keeps only its letters and digits (ACC-SINV-2026-00001 -> RF..ACCSINV202600001), so
    the reference fits the 25 characters of the guidelines; a longer number is refused.
    """
    body = re.sub(r"[^A-Z0-9]", "", (invoice_name or "").upper())
    if not body or len(body) > 21:
        raise ValueError(
            f"invoice number {invoice_name} gives no creditor reference of 1 to 21 characters"
        )
    # the mod 97 check of the IBAN, on the number moved to the end with RF00 in its place
    digits = "".join(str(int(c, 36)) for c in body + "RF00")
    return f"RF{98 - int(digits) % 97:02d}{body}"


def reference_text(reference):
    """The reference in groups of four, as the slip prints it (SIX); the QR text keeps it unspaced."""
    return " ".join(reference[i : i + 4] for i in range(0, len(reference), 4))
