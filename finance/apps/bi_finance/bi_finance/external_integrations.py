"""One site switch and HTTP boundary for outside integrations.

Import Frappe only when checking the site, so the clients remain importable on
hosts without Frappe. Offline client tests explicitly replace the site check.
"""

import urllib.request

MESSAGE = "External integrations are disabled by site configuration (disable_external_integrations)."


def check(scheduled=False):
    """Refuse manual calls, or log a scheduled skip. Return whether calls may proceed."""
    import frappe

    if not frappe.conf.get("disable_external_integrations"):
        return True
    if scheduled:
        logger = frappe.logger("bi_finance")
        logger.setLevel("INFO")
        logger.info(MESSAGE)
        return False
    frappe.throw(frappe._(MESSAGE))


def open_request(request, timeout, opener=None):
    """Check immediately before each HTTP call, including injected test openers."""
    check()
    return (opener or urllib.request.urlopen)(request, timeout=timeout)
