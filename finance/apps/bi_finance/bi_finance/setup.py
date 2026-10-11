"""Site changes that must survive a migrate, run after every migrate (hooks.py, after_migrate).

A fixture holds our own Property Setter rows, but erpnextswiss ships its own fixtures too: on
every migrate its print formats come back enabled, and they render the QR code through
data.libracore.ch, which sends invoice data to that host when a slip is printed
(finance/docs/swiss.md, Risks 1). So after the migrate the formats naming that host are
switched off again. Idempotent: a format already disabled is left alone and nothing is said.
"""

import frappe

LIBRACORE_HOST = "data.libracore.ch"


def disable_libracore_print_formats():
    """Print Formats whose HTML names data.libracore.ch: disabled = 1. Returns the names it switched."""
    rows = frappe.get_all(
        "Print Format",
        filters={"disabled": 0, "html": ["like", f"%{LIBRACORE_HOST}%"]},
        pluck="name",
    )
    for name in rows:
        frappe.db.set_value("Print Format", name, "disabled", 1, update_modified=False)
    if rows:
        print(f"bi_finance: disabled {len(rows)} print format(s) rendering through {LIBRACORE_HOST}: {', '.join(rows)}")
    return rows


def after_migrate():
    disable_libracore_print_formats()
