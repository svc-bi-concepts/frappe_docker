"""Stores auto_level_enabled = 1 on an AI Assistant Settings single saved before the field existed.

Frappe fills a single's defaults only while it has no stored row at all, and reads a missing Check as 0, so a site that
saved the OpenRouter key before the Actions section would read level 0 as off. Idempotent: a stored value, set or
cleared since, is left as it is.
"""

import frappe

SETTINGS = "AI Assistant Settings"
FIELD = "auto_level_enabled"


def execute():
    stored = {
        row[0]
        for row in frappe.db.sql(
            "select field from `tabSingles` where doctype = %s", (SETTINGS,)
        )
    }
    if not stored or FIELD in stored:
        return
    frappe.db.set_single_value(SETTINGS, FIELD, 1)
    print(f"ai_auto_level_default: {FIELD} set on {SETTINGS}")
