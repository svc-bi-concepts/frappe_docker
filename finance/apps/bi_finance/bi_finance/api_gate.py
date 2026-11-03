"""The method gate of the API key users (erp-kb06): api-sync and api-mcp call only the whitelisted methods named here.

Whitelisted functions answer any logged-in user unless their own body checks a permission, and
frappe.desk.search.search_link calls a whitelisted query by name, so refusing the methods found one by one leaves
whatever was not read open. An allow list fails closed: a new or unread method answers 403 until it is added below
(code, next image). Resources (/api/resource, /api/v2/document) and document methods are not gated: Frappe checks
the document's permission there, and the key roles are what limits them (keyaccess in swiss-setup.py).

The hook is auth_hooks, not before_request: Frappe authenticates a token in validate_auth(), which runs after
init_request and its before_request hooks, so a before_request hook sees Guest for every key. auth_hooks run at the
end of validate_auth(), with the key's user set, for token and cookie requests alike.

api-agent is not gated: the importers stay on it until erp-tamd. Humans and Guest pass unchanged.
"""

import frappe

GATED_USERS = frozenset({"api-sync@finance.local", "api-mcp@finance.local"})

# Exact dotted names, no prefix match. What the hourly sync calls (getdoctype, the meta its loaders read), what
# frappe-mcp-server 1.2.0 calls itself (ping) or names for the model (get_logged_user), run for the ledger reports an
# MCP session asks through frappe_run_method (Trial Balance, General Ledger, Profit and Loss, Balance Sheet, the
# ageing reports), and get_script, the one report call the key check asks: it passes the same report role gate as
# run and runs no report. Each reads as the session user and checks that user's permission; none writes, none is in
# frappe.desk.search (it calls any whitelisted query by name), none runs a document method, hrms or erpnextswiss
# (employee_query and create_payment_proposal_for_mcp answer without a payroll role). No bi_finance method: neither
# the sync nor an MCP tool calls one.
ALLOWED_METHODS = frozenset(
    {
        "ping",  # the MCP's ping tool, /api/method/ping
        "frappe.ping",
        "frappe.auth.get_logged_user",
        "frappe.desk.form.load.getdoctype",
        "frappe.desk.query_report.get_script",
        "frappe.desk.query_report.run",
    }
)

# /api/method/<m> and /api/v1/method/<m>: Frappe cuts the name at the first "/" (backward compatibility)
V1_PREFIXES = ("/api/method/", "/api/v1/method/")
V2_PREFIX = "/api/v2/method/"
MAX_SHOWN = 200


def method_of(path, cmd=None):
    """The whitelisted method a request would call, or None when it calls none (a resource or a document route).

    Frappe looks at cmd first, on any path (app.py), so a cmd in the query string or the body names the method even
    on /api/resource/.... A name that cannot be on the list (a non-string cmd, a v2 /method/<doctype>/<method>
    route, which Frappe resolves to a controller module) is returned as it is and so refused.
    """
    if cmd:
        return cmd if isinstance(cmd, str) else "<cmd that is no string>"
    for prefix in V1_PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix) :].split("/")[0]
    if path.startswith(V2_PREFIX):
        rest = path[len(V2_PREFIX) :]
        # one trailing slash is allowed by the router; a further "/" is the doctype route, never a listed name
        return rest[:-1] if rest.endswith("/") else rest
    return None


def refuse():
    """auth_hooks: raise PermissionError (HTTP 403) for a gated user calling a method that is not listed."""
    if frappe.session.user not in GATED_USERS:
        return
    method = method_of(frappe.request.path, frappe.form_dict.get("cmd"))
    if method is None or method in ALLOWED_METHODS:
        return
    raise frappe.PermissionError(
        f"Method {method[:MAX_SHOWN]} is not allowed for this key"
    )
