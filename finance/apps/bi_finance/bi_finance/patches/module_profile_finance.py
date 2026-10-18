"""Sets the Module Profile "Finance" on every enabled System User, hiding the modules the
company never uses from the desk navigation.

frappe's module block is navigation reach only (frappe/utils/modules.py, is_module_visible:
"Hiding a module hides the way to a document, never the document itself"): a blocked module's
workspace vanishes from the boot and the search bar, while its doctypes still open by route and
link, so Item (a Stock doctype) stays reachable from a Sales Invoice. The profile is upserted from
BLOCKED_MODULES and assigned only to enabled System Users that carry no profile of their own
(Administrator keeps the full view for repair); a
user with a profile, and a profile whose rows already match the constant, are left alone.
Idempotent: a second run changes nothing and prints nothing.
"""

import frappe

PROFILE_NAME = "Finance"

# The Module Defs hidden from the desk (live count 2026-10-18, bead erp-22x2). Kept: the modules
# with documents of ours or in plan (Accounts, Selling, Buying, Setup, HR, Payroll, HR Setup,
# Tax and Benefits, Expenses, ...) and frappe's plumbing (Core, Desk, Custom, Geo, Users, System,
# Workflow). A module here whose workspace sat on the desk loses it (Stock, CRM, Projects,
# Manufacturing, Support, Website, Assets, the unused hrms modules); a module here without a
# workspace loses nothing visible (Regional, Portal, Telephony, ...). Taking a module back is
# removing it from this list and migrating, or clearing the user's Module Profile.
BLOCKED_MODULES = [
    "Assets",
    "Automation",
    "Build",
    "Bulk Transaction",
    "Communication",
    "CRM",
    "Data",
    "EDI",
    "ERPNext Integrations",
    "Integrations",
    "Leaves",
    "Maintenance",
    "Manufacturing",
    "Performance",
    "Portal",
    "Projects",
    "Quality Management",
    "Recruitment",
    "Regional",
    "Scripts",
    "Shift and Attendance",
    "Stock",
    "Subcontracting",
    "Support",
    "Telephony",
    "Tenure",
    "Utilities",
    "Website",
]


def upsert_profile():
    """Create or update the profile from BLOCKED_MODULES; None when the rows already match."""
    rows = [{"module": m} for m in BLOCKED_MODULES]
    name = frappe.db.exists("Module Profile", PROFILE_NAME)
    if not name:
        doc = frappe.get_doc(
            {
                "doctype": "Module Profile",
                "module_profile_name": PROFILE_NAME,
                "block_modules": rows,
            }
        )
        doc.insert(ignore_permissions=True)
        return "created"
    doc = frappe.get_doc("Module Profile", name)
    if [d.module for d in doc.block_modules] != BLOCKED_MODULES:
        doc.set("block_modules", [])
        for m in BLOCKED_MODULES:
            doc.append("block_modules", {"module": m})
        doc.save(ignore_permissions=True)
        return "updated"
    return None


def assign_to_users():
    """The enabled System Users without a profile get PROFILE_NAME (its rows are copied onto the
    user by User.validate_allowed_modules); users with a profile of their own are left alone, and
    so is Administrator (its block list is no longer unioned in site-wide, it keeps the full view
    for repair). Guest is a Website User and never matches.
    """
    names = frappe.get_all(
        "User",
        filters={
            "enabled": 1,
            "user_type": "System User",
            "name": ("!=", "Administrator"),
            "module_profile": ("is", "not set"),
        },
        pluck="name",
    )
    for name in names:
        user = frappe.get_doc("User", name)
        user.module_profile = PROFILE_NAME
        user.save(ignore_permissions=True)
    return names


def execute():
    verb = upsert_profile()
    users = assign_to_users()
    if verb or users:
        print(
            f"module_profile_finance: profile {verb or 'unchanged'}, {len(users)} users assigned"
        )
        # the blocked modules ride in the boot, which is cached per user
        frappe.clear_cache()
