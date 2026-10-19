"""Stores the Actions-section defaults on an AI Assistant Settings single saved before the section existed.

Frappe fills a single's defaults only while it has no stored row at all. It reads a missing Check as 0, and the first
save stores a missing Float, Int or Currency as 0. So a site that saved the OpenRouter key before the Actions section
would read level 0 as off and the thresholds, the one-click amount limit and the daily cap as 0. Idempotent: a stored
value, set or cleared since, is left as it is.
"""

import frappe
from bi_finance import assistant_levels as levels

SETTINGS = "AI Assistant Settings"
DEFAULTS = {
    "auto_level_enabled": 1,
    "auto_threshold": levels.AUTO_THRESHOLD,
    "one_click_threshold": levels.ONE_CLICK_THRESHOLD,
    "one_click_amount_limit": levels.ONE_CLICK_AMOUNT_LIMIT,
    "auto_actions_per_day": levels.AUTO_ACTIONS_PER_DAY,
    "ledger_actions_from": levels.LEDGER_ACTIONS_FROM_DEFAULT,
}


def execute():
    stored = {
        row[0]
        for row in frappe.db.sql(
            "select field from `tabSingles` where doctype = %s", (SETTINGS,)
        )
    }
    if not stored:
        return
    for field, value in DEFAULTS.items():
        if field not in stored:
            frappe.db.set_single_value(SETTINGS, field, value)
            print(f"ai_auto_level_default: {field} set on {SETTINGS}")
