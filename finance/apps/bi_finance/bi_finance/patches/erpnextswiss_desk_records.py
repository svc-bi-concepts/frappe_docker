"""Deletes the erpnextswiss desk rows that the image no longer ships, once per site.

The build removes erpnextswiss's workspace, desktop_icon and workspace_sidebar folders (Containerfile), so migrate no
longer recreates the rows, but the rows already on the site stay until this runs: a sync never deletes a document whose
file went away. The Desktop Icon and the Workspace Sidebar archive row hold link fields to the workspaces, so they go
first and the workspaces follow with force. bi_finance's own rows carry app bi_finance and are not matched. Idempotent:
a second run finds nothing and prints nothing.
"""

import frappe

# (doctype, filters) in deletion order: the icon and the sidebar item point at the workspaces
TARGETS = [
    ("Desktop Icon", {"app": "erpnextswiss"}),
    ("Workspace Sidebar", {"app": "erpnextswiss"}),
    ("Workspace", {"module": "ERPNextSwiss"}),
]


def execute():
    counts = []
    for doctype, filters in TARGETS:
        names = frappe.get_all(doctype, filters=filters, pluck="name")
        for name in names:
            frappe.delete_doc(
                doctype, name, ignore_permissions=True, force=True, ignore_missing=True
            )
        counts.append(len(names))
    if any(counts):
        print(
            f"erpnextswiss_desk_records: deleted {counts[0]} Desktop Icon, {counts[1]} Workspace Sidebar, "
            f"{counts[2]} Workspace"
        )
    # the boot and the desktop icons are cached per user, so the deleted rows would linger until a cache clear
    frappe.clear_cache()
