"""Deletes the seven Treasury Number Cards that carried their document names, once per site.

Migrate syncs the renamed cards from number_card/ and the Treasury workspace from its file, and a sync never deletes a
standard document whose file went away, so the old cards stay until this runs. It runs after the model sync, when the
workspace already names the new cards and nothing links the old ones. A card that is already gone is skipped.
"""

import frappe

OLD_NAMES = [
    "Cash Position Total",
    "Cash Position 1000",
    "Cash Position 1010",
    "Cash Position 1020",
    "Cash Position 1021",
    "Cash Conversion CCC",
    "Cash Conversion Change",
]


def execute():
    for name in OLD_NAMES:
        frappe.delete_doc("Number Card", name, ignore_missing=True)
