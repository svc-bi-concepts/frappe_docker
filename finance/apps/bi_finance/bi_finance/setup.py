"""Site changes that must survive a migrate, run after every migrate (hooks.py, after_migrate).

A fixture holds our own Property Setter rows, but erpnextswiss ships its own fixtures too: on
every migrate its print formats come back enabled, and they render the QR code through
data.libracore.ch, which sends invoice data to that host when a slip is printed
(finance/docs/swiss.md, Risks 1). So after the migrate those formats are switched off again.
Three ways to find them: by name (the two erpnextswiss QR formats), by the host in the HTML, and
by the QR helper's endpoint in the HTML (one format reaches it through another host). Idempotent:
a format already disabled is left alone and nothing is said.
"""

import frappe

LIBRACORE_HOST = "data.libracore.ch"
# erpnextswiss's QR endpoint (phpqrcode/api/iso20022.php), on libracore's host and on the others.
QR_HELPER = "api/iso20022.php"
# erpnextswiss's two QR formats, by their public names.
QR_FORMATS = ["QR Sales Invoice", "QR Sales Invoice Switzerland Rounding - Description Details"]


def disable_libracore_print_formats():
    """Enabled erpnextswiss QR formats, or any whose HTML names the host or the helper: disabled = 1.

    Returns the names it switched.
    """
    rows = frappe.get_all(
        "Print Format",
        filters={"disabled": 0},
        or_filters=[
            ["Print Format", "name", "in", QR_FORMATS],
            ["Print Format", "html", "like", f"%{LIBRACORE_HOST}%"],
            ["Print Format", "html", "like", f"%{QR_HELPER}%"],
        ],
        pluck="name",
    )
    for name in rows:
        frappe.db.set_value("Print Format", name, "disabled", 1, update_modified=False)
    if rows:
        print(f"bi_finance: disabled {len(rows)} erpnextswiss QR print format(s): {', '.join(rows)}")
    return rows


def after_migrate():
    disable_libracore_print_formats()
