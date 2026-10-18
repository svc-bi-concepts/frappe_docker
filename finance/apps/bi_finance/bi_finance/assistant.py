"""The desk assistant, read part: what is open for the logged-in user, answered through OpenRouter.

The whitelisted ask(message, history) runs the fixed collectors below as the session user (frappe.get_list, so the
user's own permissions decide what they see; a doctype they may not read is left out instead of failing the ask),
hands their result plus the question to OpenRouter (assistant_client, zero-retention endpoints only) and returns the
answer as structured JSON: the text and the documents to link as /app/<doctype>/<name>. Nothing is written anywhere;
this bead reads. Part 2 (erp-00z0, actions with an approval ladder) registers more collectors with the same
decorator and takes its proposals from the same shape.

The key is read with get_password, travels in the request header and nowhere else: never logged, never in a
response, never in the browser. Counts and the top ten each, never whole tables (mem-4v01). bexio imports run
outside ERPNext and keep no sync log doctype, so the sync collector reports the feeds ERPNext holds only.
"""

import datetime
import urllib.parse

import frappe
from bi_finance import assistant_client
from frappe import _

DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"  # the id as OpenRouter lists it (dot, not dash: -5-5 is a 400)
DEFAULT_MAX_TOKENS = 1200

TOP = 10  # the top ten each, never whole tables (mem-4v01)
HISTORY_MESSAGES = (
    12  # the turns kept, so a long thread cannot grow one request without end
)
MESSAGE_CHARS = 4000  # one question's size cap
FIELD_CHARS = 200  # one row's free-text field in the data, so one long text cannot bloat the prompt

# the drafts a finance desk reviews before submitting, one collector over all of them
DRAFT_DOCTYPES = (
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
    "Salary Slip",
    "VAT Declaration",
)

# the feeds whose last sync ERPNext holds (a Datetime on the settings doctype); the user must be
# able to read a settings doctype for its stamp to be reported
FEED_SETTINGS = (("Wise Settings", "Wise"), ("PayPal Settings", "PayPal"))

DUE_SOON_DAYS = 14

# the registry part 2 extends: each entry {"key", "label", "run"}, run(user, today) -> a dict with
# count, top and whatever else the prompt should carry
COLLECTORS = []


def collector(key, label):
    """Register a collector under a stable key: the decorator part 2's collectors are added with."""

    def wrap(run):
        COLLECTORS.append({"key": key, "label": label, "run": run})
        return run

    return wrap


class NotPermitted(Exception):
    """The session user may not read a doctype a collector looks at: the collector reports nothing."""


@frappe.whitelist()
def ask(message, history=None):
    """One answer for the session user: {"text", "items": [{"doctype", "name", "link"}]}.

    Throws a message that says what to do when the assistant is off, when the key is missing and when OpenRouter
    fails; the thrown text is the only thing the page sees of a failure. The collectors' result and the question are
    the request's payload; the key travels in its header alone.
    """
    message = _message(message)
    settings = frappe.get_single("AI Assistant Settings")
    if not settings.enabled:
        frappe.throw(
            _("The assistant is switched off. Switch it on in AI Assistant Settings.")
        )
    key = settings.get_password("openrouter_api_key", raise_exception=False)
    if not key:
        frappe.throw(
            _("There is no OpenRouter API key yet. Enter it in AI Assistant Settings.")
        )
    try:
        answer = assistant_client.answer(
            key=key,
            model=settings.model or DEFAULT_MODEL,
            max_tokens=int(settings.max_tokens_per_answer or 0) or DEFAULT_MAX_TOKENS,
            message=message,
            history=_history(history),
            data=collect(),
        )
    except assistant_client.AssistantError as err:
        frappe.throw(str(err))
    return {"text": answer["text"], "items": _items(answer["items"])}


def collect(today=None, user=None, registry=None):
    """Every collector's result, read as the session user: the DATA block the prompt carries.

    today and user are the clock and the person of the run; a test passes both so the reads are pinned. registry
    replaces the built-in list when given (an empty list means none), so part 2 can run a subset.
    """
    today = today or frappe.utils.getdate()
    user = user or frappe.session.user
    entries = COLLECTORS if registry is None else registry
    return [_one(entry, user, today) for entry in entries]


def _one(entry, user, today):
    """One collector's result under its key and label, or the empty shape when a read is not permitted."""
    try:
        result = entry["run"](user=user, today=today)
    except NotPermitted:
        result = {
            "note": "you may not read everything this looks at, so it is left out"
        }
    return {
        "key": entry["key"],
        "label": entry["label"],
        "count": result.get("count"),
        "top": result.get("top", []),
        **{k: v for k, v in result.items() if k not in ("count", "top")},
    }


def _get_list(doctype, **kwargs):
    """frappe.get_list, so the session user's permissions apply; a doctype they may not read raises NotPermitted."""
    try:
        return frappe.get_list(doctype, **kwargs)
    except frappe.PermissionError:
        raise NotPermitted(doctype) from None


def _count(doctype, filters):
    """How many rows the filters give a user who may read the doctype: one aggregate row, never the table."""
    rows = _get_list(
        doctype, filters=filters, fields=["count(name) as n"], limit_page_length=1
    )
    return int(rows[0]["n"] or 0) if rows else 0


@collector("todos", "Open to-dos assigned to you")
def open_todos(user, today):
    """The user's open ToDos, oldest due date first, with the document each is about."""
    filters = {"status": "Open", "allocated_to": user}
    rows = _get_list(
        "ToDo",
        filters=filters,
        fields=[
            "name",
            "description",
            "priority",
            "date",
            "reference_type",
            "reference_name",
        ],
        order_by="date asc, modified desc",
        limit_page_length=TOP,
    )
    for row in rows:
        # the description is a Text Editor field: its markup is taken off and the rest capped
        row["description"] = _text(
            frappe.utils.strip_html(row.get("description") or ""), FIELD_CHARS
        )
    return {"count": _count("ToDo", filters), "top": rows}


@collector("drafts", "Drafts waiting to be reviewed and submitted")
def drafts(user, today):
    """The drafts of the finance doctypes, per doctype the count and the ten most recently touched."""
    by_doctype = {}
    top = []
    for doctype in DRAFT_DOCTYPES:
        filters = {"docstatus": 0}
        by_doctype[doctype] = _count(doctype, filters)
        if by_doctype[doctype]:
            top += [
                {"doctype": doctype, "name": row["name"]}
                for row in _get_list(
                    doctype,
                    filters=filters,
                    fields=["name"],
                    order_by="modified desc",
                    limit_page_length=TOP,
                )
            ]
    return {"count": sum(by_doctype.values()), "by_doctype": by_doctype, "top": top}


@collector(
    "sales_overdue", "Sales Invoices overdue (submitted, unpaid, past the due date)"
)
def overdue_sales_invoices(user, today):
    filters = {
        "docstatus": 1,
        "outstanding_amount": [">", 0],
        "due_date": ["<", today],
    }
    return {
        "count": _count("Sales Invoice", filters),
        "top": _get_list(
            "Sales Invoice",
            filters=filters,
            fields=["name", "customer", "due_date", "outstanding_amount"],
            order_by="due_date asc, modified desc",
            limit_page_length=TOP,
        ),
    }


@collector(
    "purchase_due",
    "Purchase Invoices due within 14 days (submitted, unpaid, overdue ones in)",
)
def purchase_invoices_due(user, today):
    horizon = today + datetime.timedelta(days=DUE_SOON_DAYS)
    filters = {
        "docstatus": 1,
        "outstanding_amount": [">", 0],
        "due_date": ["<=", horizon],
    }
    return {
        "count": _count("Purchase Invoice", filters),
        "top": _get_list(
            "Purchase Invoice",
            filters=filters,
            fields=["name", "supplier", "due_date", "outstanding_amount"],
            order_by="due_date asc, modified desc",
            limit_page_length=TOP,
        ),
    }


@collector("bank_unreconciled", "Bank Transactions not reconciled, per account")
def unreconciled_bank_transactions(user, today):
    """The Unreconciled Bank Transactions, one count per account; the top lists the accounts, not the rows."""
    rows = _get_list(
        "Bank Transaction",
        filters={"docstatus": ["<", 2], "status": "Unreconciled"},
        fields=["bank_account", "count(name) as n"],
        group_by="bank_account",
        order_by="bank_account asc",
        limit_page_length=0,
    )
    counts = [
        {"bank_account": row["bank_account"], "n": int(row["n"] or 0)} for row in rows
    ]
    return {"count": sum(c["n"] for c in counts), "top": counts}


@collector("vat_declaration", "VAT Declaration of the last closed quarter")
def current_vat_declaration(user, today):
    """Whether the last closed quarter's declaration exists (a draft or submitted one that covers it).

    The covering read takes a half-year or year declaration too, so the fill's own period shapes are all seen; the
    dates of the quarter are reported either way.
    """
    start, end = _last_closed_quarter(today)
    filters = {
        "docstatus": ["<", 2],
        "start_date": ["<=", start],
        "end_date": [">=", end],
    }
    rows = _get_list(
        "VAT Declaration",
        filters=filters,
        fields=["name", "docstatus"],
        limit_page_length=TOP,
    )
    return {
        "count": len(rows),
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "missing": not rows,
        "top": rows,
    }


@collector("payroll_month", "Salary Slips of the current month")
def payroll_month(user, today):
    """The month's Salary Slips: submitted and draft counts, the ten most recently touched."""
    first = today.replace(day=1)
    nxt = datetime.date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    bounds = [["start_date", ">=", first], ["start_date", "<=", last]]
    submitted = _count("Salary Slip", [["docstatus", "=", 1]] + bounds)
    drafts = _count("Salary Slip", [["docstatus", "=", 0]] + bounds)
    return {
        "count": submitted + drafts,
        "month": first.isoformat()[:7],
        "submitted": submitted,
        "drafts": drafts,
        "top": _get_list(
            "Salary Slip",
            filters=[["docstatus", "<", 2]] + bounds,
            fields=["name", "employee", "employee_name", "docstatus"],
            order_by="modified desc",
            limit_page_length=TOP,
        ),
    }


@collector("syncs", "Last sync of the bank feeds")
def sync_results(user, today):
    """Each feed's last sync, when the user may read its settings doctype; bexio keeps no sync log doctype."""
    rows = []
    for doctype, feed in FEED_SETTINGS:
        if not frappe.has_permission(doctype, "read", user=user):
            continue
        stamp = frappe.db.get_single_value(doctype, "last_sync")
        rows.append({"feed": feed, "last_sync": stamp})
    return {
        "count": sum(1 for row in rows if row["last_sync"]),
        "top": rows,
        "note": "bexio imports outside ERPNext keep no sync log, so bexio reports nothing here",
    }


def _last_closed_quarter(today):
    """The quarter before today's: its first and last day, a declaration covering those bounds is the current one."""
    first_of_quarter = datetime.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    end = first_of_quarter - datetime.timedelta(days=1)
    start = datetime.date(end.year, 3 * ((end.month - 1) // 3) + 1, 1)
    return start, end


def _message(message):
    """The question as one string of at most MESSAGE_CHARS; an empty one is refused before anything is read."""
    text = _text(message, MESSAGE_CHARS)
    if not text:
        frappe.throw(_("Type a question first."))
    return text


def _history(history):
    """The past turns as the request carries them: the last HISTORY_MESSAGES, user and assistant only, each capped."""
    if isinstance(history, str):
        history = frappe.parse_json(history)
    if not isinstance(history, list):
        return []
    turns = []
    for turn in history[-HISTORY_MESSAGES:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = _text(turn.get("content"), MESSAGE_CHARS)
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns


def _items(items):
    """The answer's items as the page links them: known doctypes with existing documents, each once, at most TOP.

    The model's strings are cleaned and capped, the doctype and the document are both checked against the database,
    and the link is built here the way the desk spells routes: /app/<doctype slug>/<name>. What the model invents is
    dropped, not linked; the scan stops once TOP are collected, so a long answer cannot turn into many lookups.
    """
    out = []
    seen = set()
    known = {}
    for item in items or []:
        if len(out) >= TOP:
            break
        if not isinstance(item, dict):
            continue
        doctype = _text(item.get("doctype"), 140)
        name = _text(item.get("name"), 140)
        if not doctype or not name or (doctype, name) in seen:
            continue
        seen.add((doctype, name))
        if doctype not in known:
            known[doctype] = bool(frappe.db.exists("DocType", doctype))
        if not known[doctype] or not frappe.db.exists(doctype, name):
            continue
        out.append({"doctype": doctype, "name": name, "link": _link(doctype, name)})
    return out[:TOP]


def _link(doctype, name):
    """The document's route: the doctype slugged the way frappe.router.slug does, the name URL-quoted."""
    return "/app/{}/{}".format(
        doctype.lower().replace(" ", "-"),
        urllib.parse.quote(name, safe=""),
    )


def _text(value, chars):
    """A piece of text as one line of at most chars: the model's output and the rows' free text both."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:chars]
