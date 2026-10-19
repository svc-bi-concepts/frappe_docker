"""Creates the Role "AI Approver", who approves the desk assistant's level 2 actions on the AI Action form.

The role is never given to the API user (api-agent@finance.local): assistant_actions.approve refuses that user
whatever its roles. Idempotent: a second run finds the role and prints nothing.
"""

import frappe

ROLE = "AI Approver"


def execute():
    if frappe.db.exists("Role", ROLE):
        return
    frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(
        ignore_permissions=True
    )
    print(f"ai_approver_role: role {ROLE} created")
