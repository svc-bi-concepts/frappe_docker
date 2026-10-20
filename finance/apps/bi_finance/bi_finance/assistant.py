"""The desk assistant, read part: what is open for the logged-in user, answered through OpenRouter.

The whitelisted ask(message, history) runs the fixed collectors below as the session user (frappe.get_list, so the
user's own permissions decide what they see; a doctype they may not read is left out instead of failing the ask),
hands their result plus the question to OpenRouter (assistant_client, zero-retention endpoints only) and returns the
answer as structured JSON: the text and the documents to link as /app/<doctype>/<name>. Nothing is written anywhere;
this bead reads. Part 2 (erp-00z0, actions with an approval ladder) registers more collectors with the same
decorator and takes its proposals from the same shape.

Part 2b (erp-wcf3) lets the model propose: with actions_enabled on, ask() offers the registry's types as tools
(build_tools: the types' own schemas plus task and confidence, nothing else), sends every tool call the model returns
through assistant_actions.propose as the session user, and returns the resulting AI Actions as actions[] cards. The
whitelisted approve_action, reject_action and undo_action take an AI Action name and nothing else; the user is always
the session's, and assistant_actions decides whether that user may.

The key is read with get_password, travels in the request header and nowhere else: never logged, never in a
response, never in the browser. Counts and the top ten each, never whole tables (mem-4v01). bexio imports run
outside ERPNext and keep no sync log doctype, so the feed collector reports the feeds ERPNext holds only.

The collector keys are the task ids of the bookkeeper's plan (erp-00z0): bank.lines, bank.feeds, sales.overdue,
drafts, bills.due, expenses.approve, month.revaluation, month.close, vat.quarter, payroll.month; todos is the user's own
list, not a task of the plan, yet a tool call may name it to close one of them. While today is before the ledger date (AI Assistant Settings.ledger_actions_from,
default 2027-01-01) every answer starts with one line: bexio leads, ERPNext is the parallel copy, nothing is
booked before then.
"""

import collections
import datetime
import json
import urllib.parse

import frappe
from bi_finance import assistant_actions, assistant_client, bank_match
from bi_finance.assistant_levels import LEDGER_ACTIONS_FROM_DEFAULT
from frappe import _

DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"  # the id as OpenRouter lists it (dot, not dash: -5-5 is a 400)
DEFAULT_MAX_TOKENS = 1200

TOP = 10  # the top ten each, never whole tables (mem-4v01)
HISTORY_MESSAGES = 24
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

# drafts of doctypes another app brings (Payment Proposal: erpnextswiss, Expense Claim: HRMS); counted only when
# the table is there, as payment_run.create_payment_proposal reads Expense Claim (frappe.db.table_exists)
OPTIONAL_DRAFT_DOCTYPES = ("Payment Proposal", "Expense Claim")

# the drafts month.close counts by their date field; VAT Declaration stays with vat.quarter
MONTH_DATED_DRAFTS = (
    ("Sales Invoice", "posting_date"),
    ("Purchase Invoice", "posting_date"),
    ("Payment Entry", "posting_date"),
    ("Journal Entry", "posting_date"),
    ("Payment Proposal", "date"),
    ("Expense Claim", "posting_date"),
    ("Salary Slip", "start_date"),
    ("Exchange Rate Revaluation", "posting_date"),
)

# the feeds whose sync_enabled and last_sync ERPNext holds (fields on the settings doctype); the user must be
# able to read a settings doctype for its stamps to be reported
FEED_SETTINGS = (("Wise Settings", "Wise"), ("PayPal Settings", "PayPal"))

# a feed sync runs hourly; an older last_sync (or sync_enabled off) is flagged
FEED_STALE_HOURS = 3

# the doctypes behind one bank.lines answer: the lines and the match job's own reads. The user must be able to read
# every one of them or the collector reports nothing
BANK_LINE_DOCTYPES = (
    "Bank Transaction",
    "Bank Account",
    "Account",
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Bank Transaction Rule",
)

DUE_SOON_DAYS = 14

# what each task may propose, from the task table of erp-00z0; DATA names a task's types that are registered, so the
# ledger types show up as soon as erp-9f7y registers them
TASK_ACTIONS = {
    "bank.lines": ("match_bank_transaction",),
    "sales.overdue": ("create_todo", "add_comment"),
    "drafts": ("add_comment",),
    "bills.due": ("create_payment_proposal",),
    "month.revaluation": ("submit_revaluation",),
    "vat.quarter": ("create_vat_draft", "submit_vat_declaration"),
    "todos": ("close_todo",),
}

# the tasks a tool call may name: the task table's ids and the user's own todos
TOOL_TASKS = assistant_client.TASK_IDS + ("todos",)

# bexio leads until the ledger date (the bookkeeper's D1); the line every answer starts with before it
BANNER_TEXT = (
    "bexio leads until {date}; ERPNext is the parallel copy, "
    "the assistant books nothing before then."
)

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


@frappe.whitelist(methods=["POST"])
def ask(message, history=None):
    """One answer for the session user: {"text", "items": [{"doctype", "name", "link"}], "actions": [card]}.

    actions holds one card per tool call the model made (see _card); it is empty unless actions_enabled is on.

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
    banner = _ledger_banner(settings)
    tools = build_tools() if getattr(settings, "actions_enabled", 0) else None
    try:
        data = collect()
        answer = assistant_client.answer(
            key=key,
            model=settings.model or DEFAULT_MODEL,
            max_tokens=int(settings.max_tokens_per_answer or 0) or DEFAULT_MAX_TOKENS,
            message=message,
            history=_history(history),
            data=data,
            banner=banner,
            tools=tools,
        )
    except assistant_client.AssistantError as err:
        frappe.throw(str(err))
    cards = [_propose(call) for call in (answer.get("calls") or [])] if tools else []
    text = answer["text"]
    if not text.strip() and cards:
        text = _("I proposed {0} action(s). They are below.").format(len(cards))
    # the model is told the line; should it drop or change it, it is put in front here, never twice
    if banner and not text.startswith(banner):
        text = banner + "\n" + text
    return {"text": text, "items": _items(answer["items"], data), "actions": cards}


@frappe.whitelist(methods=["POST"])
def approve_action(action):
    """Level 1: approve one AI Action as the session user; the card as it is afterwards.

    No user name comes from the client: assistant_actions refuses unless the session user is the one who asked.
    Level 2 is approved on the AI Action form (erp-ss1j), where the amount is typed; here it is refused.
    """
    return _decide(action, assistant_actions.approve)


@frappe.whitelist(methods=["POST"])
def reject_action(action):
    """Turn down a proposed AI Action as the session user (who asked, or an AI Approver); the card afterwards."""
    return _decide(action, assistant_actions.reject)


@frappe.whitelist(methods=["POST"])
def undo_action(action):
    """Take back an auto-done AI Action as the session user; the card afterwards."""
    return _decide(action, assistant_actions.undo)


def _decide(action, step):
    """Run approve, reject or undo for the named AI Action as the session user and answer with its card."""
    if not isinstance(action, str) or not action.strip():
        frappe.throw(_("Name the AI Action."))
    name = action.strip()
    user = frappe.session.user
    # the names run in sequence, so a stranger is turned away before the step: it would answer with the card (checks,
    # target, dry run text) and write its refusal into a row that is not theirs
    if not assistant_actions._may_manage(frappe.get_doc("AI Action", name), user):
        frappe.throw(_("This AI Action is not yours."))
    return _card(step(name, user))


def build_tools(registry=None):
    """The tools the model is offered: one function per registered action type, nothing else.

    Each takes the type's own argument schema plus task (the task id it serves) and confidence (the model's score
    from 0 to 1), both required. registry replaces ACTIONS when given, so a test can pin the list.
    """
    tools = []
    for name, spec in (
        assistant_actions.ACTIONS if registry is None else registry
    ).items():
        parameters = json.loads(
            json.dumps(spec.schema)
        )  # a copy: the registry's schema stays as registered
        properties = parameters.setdefault("properties", {})
        properties["task"] = {
            "type": "string",
            "enum": list(TOOL_TASKS),
            "description": "The id of the task this action serves.",
        }
        properties["confidence"] = {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "How sure you are that this action is right, 0 to 1.",
        }
        parameters["required"] = list(parameters.get("required", [])) + [
            "task",
            "confidence",
        ]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.schema.get("description")
                    or "Propose the action " + name.replace("_", " ") + ".",
                    "parameters": parameters,
                },
            }
        )
    return tools


def _propose(call):
    """One tool call of the model as one AI Action: the task and the confidence come off the arguments, the rest are
    the action's own, and propose() decides, refusing and logging what it does not know or what fails its schema. A
registered type the named task does not call for (TASK_ACTIONS), or a call naming no task, is refused and logged here.
    """
    arguments = dict(call.get("arguments") or {})
    task = _text(arguments.pop("task", ""), 140)
    confidence = arguments.pop("confidence", None)
    name = _text(call.get("name"), 140)
    proposal = (frappe.session.user, task, name, arguments, confidence)
    if name in assistant_actions.ACTIONS and name not in TASK_ACTIONS.get(
        task, ()
    ):
        reason = (
            _("The task {0} does not call for {1}.").format(task, name)
            if task
            else _("The call names no task.")
        )
        return _card(assistant_actions.refuse(*proposal, reason))
    return _card(assistant_actions.propose(*proposal))


def _card(result):
    """An AI Action as the page shows it, from the result of propose, approve, reject or undo.

    dry_run: approving it books nothing (its ledger date check failed). undoable: the type has an Undo.
    """
    doc = frappe.get_doc("AI Action", result["name"])
    spec = assistant_actions.ACTIONS.get(doc.action_type)
    checks = [
        {
            "check": row.get("check"),
            "passed": bool(row.get("passed")),
            "detail": row.get("detail") or "",
        }
        for row in doc.checks or []
    ]
    return {
        "name": doc.name,
        "type": doc.action_type,
        "task": doc.task,
        "target_doctype": doc.target_doctype or "",
        "target_name": doc.target_name or "",
        "level": str(doc.level),
        "confidence": doc.confidence,
        "checks": checks,
        "state": doc.state,
        "ok": bool(result.get("ok")),
        "message": result.get("message") or doc.error or "",
        "dry_run": any(
            row["check"] == "ledger date" and not row["passed"] for row in checks
        ),
        "dry_run_result": doc.dry_run_result or "",
        "undoable": bool(spec and spec.undo),
        "form": _link("AI Action", doc.name),
    }


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
    out = {
        "key": entry["key"],
        "label": entry["label"],
        "count": result.get("count"),
        "top": result.get("top", []),
        **{k: v for k, v in result.items() if k not in ("count", "top")},
    }
    proposals = [
        name
        for name in TASK_ACTIONS.get(entry["key"], ())
        if name in assistant_actions.ACTIONS
    ]
    if proposals:
        out["proposals"] = proposals
    return out


def _get_list(doctype, **kwargs):
    """frappe.get_list, so the session user's permissions apply; a doctype they may not read raises NotPermitted."""
    try:
        return frappe.get_list(doctype, **kwargs)
    except frappe.PermissionError:
        raise NotPermitted(doctype) from None


def _count(doctype, filters):
    """How many rows the filters give a user who may read the doctype: one aggregate row, never the table."""
    rows = _get_list(
        doctype,
        filters=filters,
        fields=[{"COUNT": "name", "as": "n"}],
        limit_page_length=1,
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
    """The drafts of the finance doctypes, per doctype the count and the ten most recently touched.

    Payment Proposal and Expense Claim are counted only when their table exists (ERPNext without
    erpnextswiss or HRMS has neither).
    """
    by_doctype = {}
    top = []
    doctypes = list(DRAFT_DOCTYPES) + [
        doctype
        for doctype in OPTIONAL_DRAFT_DOCTYPES
        if frappe.db.table_exists(doctype)
    ]
    for doctype in doctypes:
        filters = {"docstatus": 0}
        try:
            count = _count(doctype, filters)
            rows = (
                _get_list(
                    doctype,
                    filters=filters,
                    fields=["name", "modified"],
                    order_by="modified desc",
                    limit_page_length=TOP,
                )
                if count
                else []
            )
        except NotPermitted:
            continue
        by_doctype[doctype] = count
        if rows:
            top += [
                {
                    "doctype": doctype,
                    "name": row["name"],
                    "modified": row.get("modified"),
                }
                for row in rows
            ]
    return {
        "count": sum(by_doctype.values()),
        "by_doctype": by_doctype,
        "top": [
            {"doctype": row["doctype"], "name": row["name"]}
            for row in sorted(top, key=lambda row: row["modified"] or "", reverse=True)[
                :TOP
            ]
        ],
    }


@collector(
    "sales.overdue", "Sales Invoices overdue (submitted, unpaid, past the due date)"
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
    "bills.due",
    "Bills due within 14 days (submitted, unpaid: Purchase Invoices, Expense Claims)",
)
def bills_due(user, today):
    """Submitted Purchase Invoices unpaid and due within the horizon, plus submitted Expense Claims still Unpaid
    when HRMS is installed (their claim names and amounts only, never the employee)."""
    horizon = today + datetime.timedelta(days=DUE_SOON_DAYS)
    filters = {
        "docstatus": 1,
        "outstanding_amount": [">", 0],
        "due_date": ["<=", horizon],
    }
    by_doctype = {"Purchase Invoice": _count("Purchase Invoice", filters)}
    rows = [
        {
            "doctype": "Purchase Invoice",
            "name": row["name"],
            "supplier": row.get("supplier"),
            "date": str(row.get("due_date") or "")[:10],
            "outstanding_amount": row.get("outstanding_amount"),
        }
        for row in _get_list(
            "Purchase Invoice",
            filters=filters,
            fields=["name", "supplier", "due_date", "outstanding_amount"],
            order_by="due_date asc, modified desc",
            limit_page_length=TOP,
        )
    ]
    if frappe.db.table_exists("Expense Claim"):
        claim_filters = {"docstatus": 1, "status": "Unpaid"}
        by_doctype["Expense Claim"] = _count("Expense Claim", claim_filters)
        rows += [
            {
                "doctype": "Expense Claim",
                "name": row["name"],
                "date": str(row.get("posting_date") or "")[:10],
                "total_claimed_amount": row.get("total_claimed_amount"),
            }
            for row in _get_list(
                "Expense Claim",
                filters=claim_filters,
                fields=["name", "posting_date", "total_claimed_amount"],
                order_by="posting_date asc, modified desc",
                limit_page_length=TOP,
            )
        ]
    return {
        "count": sum(by_doctype.values()),
        "by_doctype": by_doctype,
        "top": sorted(rows, key=lambda row: row["date"] or "")[:TOP],
    }


@collector(
    "bank.lines",
    "Bank lines not reconciled, per account, and why the recent ones stayed",
)
def bank_lines(user, today):
    """The Unreconciled Bank Transactions: per account the recent lines (this and last month) counted apart from the
    older history (most open lines are imported history), and for the recent ones only, why each stayed.

    The why is bank_match.decide run read-only on every recent line of each account with the match job's own readers,
    so the reasons are the job's and cover all recent lines: several candidates, a document two lines claim, nothing
    the rules book, nothing found; the top are the ten most recent. Recent lines only, never the older history and
    never a whole-table decide (mem-4v01). A top row carries the line's references (QR reference, EndToEndId), never
    its free-text description, which can name a person. The job's readers
    read with frappe.get_all and ask no permissions, so every doctype behind the answer must be readable, or the
    collector reports nothing.
    """
    # the job's module pulls erpnext at its import, so it is imported here and not with this module
    from bi_finance import bank_match_job

    _permitted(user, BANK_LINE_DOCTYPES)
    base = [["docstatus", "=", 1], ["status", "=", "Unreconciled"]]
    total = _count("Bank Transaction", base)
    first_of_last = _month_bounds(today)[0]
    recent_filters = base + [["date", ">=", first_of_last]]
    recent = _count("Bank Transaction", recent_filters)
    lines = _get_list(
        "Bank Transaction",
        filters=recent_filters,
        fields=bank_match_job.LINE_FIELDS + ["bank_account"],
        order_by="date desc",
        limit_page_length=0,
    )
    reasons, top = _decide_recent(lines, user)
    return {
        "count": total,
        "recent": recent,
        "older": total - recent,
        "by_account": _per_account_counts(base, first_of_last),
        "reasons": reasons,
        "top": top,
    }


@collector("vat.quarter", "VAT Declaration of the last closed quarter")
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
        "count": _count("VAT Declaration", filters),
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "missing": not rows,
        "top": rows,
    }


@collector("payroll.month", "Salary Slips of the current month")
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
        # counts and slip names only, never employee or employee_name (the review of erp-r6zb)
        "top": _get_list(
            "Salary Slip",
            filters=[["docstatus", "<", 2]] + bounds,
            fields=["name", "docstatus"],
            order_by="modified desc",
            limit_page_length=TOP,
        ),
    }


@collector("bank.feeds", "Bank feeds: last sync and the month's statement upload")
def bank_feeds(user, today):
    """Wise and PayPal: sync on or off and the last sync, flagged when off or older than FEED_STALE_HOURS; plus the
    latest Bank Statement Upload month, flagged when the last full month has none. bexio imports run outside ERPNext
    and keep no sync log doctype, so bexio reports nothing here."""
    rows = []
    flags = 0
    for doctype, feed in FEED_SETTINGS:
        if not frappe.has_permission(doctype, "read", user=user):
            continue
        enabled = bool(frappe.db.get_single_value(doctype, "sync_enabled"))
        stamp = frappe.db.get_single_value(doctype, "last_sync")
        hours = _hours_since(stamp)
        if not enabled:
            why, stale = "sync is off", True
        elif hours is None:
            why, stale = "never synced", True
        elif hours > FEED_STALE_HOURS:
            why, stale = f"last sync more than {FEED_STALE_HOURS} h ago", True
        else:
            why, stale = None, False
        flags += 1 if stale else 0
        rows.append(
            {
                "feed": feed,
                "sync_enabled": enabled,
                "last_sync": str(stamp)[:19] if stamp else None,
                "hours_since": round(hours, 1) if hours is not None else None,
                "flag": why,
            }
        )
    upload = _upload_status(today)
    flags += 1 if upload.get("upload_month_missing") else 0
    return {
        "count": flags,
        "top": rows,
        **upload,
        "note": "bexio imports outside ERPNext keep no sync log, so bexio reports nothing here",
    }


@collector("expenses.approve", "Expense Claims waiting for approval")
def expense_approvals(user, today):
    """The Expense Claims in approval, docstatus 0 and approval_status Draft or pending; claim names and amounts
    only, never the employee. Nothing when HRMS is not installed."""
    if not frappe.db.table_exists("Expense Claim"):
        return {
            "count": 0,
            "top": [],
            "note": "no Expense Claim doctype (HRMS is not installed)",
        }
    filters = {
        "docstatus": 0,
        "approval_status": ["in", ["Draft", "Pending"]],
    }
    return {
        "count": _count("Expense Claim", filters),
        "top": _get_list(
            "Expense Claim",
            filters=filters,
            fields=["name", "approval_status", "total_claimed_amount", "posting_date"],
            order_by="posting_date asc, modified desc",
            limit_page_length=TOP,
        ),
    }


@collector("month.revaluation", "Exchange Rate Revaluation drafts to review and submit")
def revaluation_drafts(user, today):
    """The Exchange Rate Revaluation drafts, the ten most recent first; a person reviews and submits them."""
    filters = {"docstatus": 0}
    return {
        "count": _count("Exchange Rate Revaluation", filters),
        "top": _get_list(
            "Exchange Rate Revaluation",
            filters=filters,
            fields=["name", "company", "posting_date"],
            order_by="posting_date desc, modified desc",
            limit_page_length=TOP,
        ),
    }


@collector("month.close", "Checklist of the last full month")
def month_close(user, today):
    """The last full month, counts only: unreconciled bank lines dated in it, drafts dated in it, and whether a
    submitted Exchange Rate Revaluation exists for it."""
    first, last = _month_bounds(today)
    bounds = [["date", ">=", first], ["date", "<=", last]]
    lines_open = _count(
        "Bank Transaction",
        [["docstatus", "=", 1], ["status", "=", "Unreconciled"]] + bounds,
    )
    drafts = {}
    for doctype, field in MONTH_DATED_DRAFTS:
        if doctype in OPTIONAL_DRAFT_DOCTYPES and not frappe.db.table_exists(doctype):
            continue
        try:
            drafts[doctype] = _count(
                doctype,
                [["docstatus", "=", 0], [field, ">=", first], [field, "<=", last]],
            )
        except NotPermitted:
            continue
    submitted = _count(
        "Exchange Rate Revaluation",
        {"docstatus": 1, "posting_date": ["between", [first, last]]},
    )
    return {
        "count": lines_open + sum(drafts.values()),
        "month": first.isoformat()[:7],
        "lines_unreconciled": lines_open,
        "drafts": drafts,
        "revaluation_submitted": bool(submitted),
        "top": [],
    }


def _permitted(user, doctypes):
    """NotPermitted unless the user may read every doctype of a combined read."""
    for doctype in doctypes:
        if not frappe.has_permission(doctype, "read", user=user):
            raise NotPermitted(doctype)


def _month_bounds(today):
    """The last full month before today's: its first and last day."""
    last_of_previous = today.replace(day=1) - datetime.timedelta(days=1)
    return last_of_previous.replace(day=1), last_of_previous


def _per_account_counts(base, first_of_last):
    """The Unreconciled Bank Transactions per account, recent ones apart from older history."""
    maps = []
    for filters in (
        base + [["date", ">=", first_of_last]],
        base + [["date", "<", first_of_last]],
    ):
        rows = _get_list(
            "Bank Transaction",
            filters=filters,
            fields=["bank_account", {"COUNT": "name", "as": "n"}],
            group_by="bank_account",
            order_by="bank_account asc",
        )
        maps.append({row["bank_account"]: int(row["n"] or 0) for row in rows})
    recent, older = maps
    return [
        {
            "bank_account": account,
            "recent": recent.get(account, 0),
            "older": older.get(account, 0),
        }
        for account in sorted(set(recent) | set(older))
    ]


def _decide_recent(lines, user):
    """bank_match.decide read-only on recent lines, with the match job's readers scoped to the user: counts by
    reason over all lines and the TOP most recent with their reason. A line whose account gives no company or currency
    is decided by nobody and carries no reason."""
    from bi_finance import bank_match_job

    reasons = {
        key: 0
        for key in (
            bank_match.SEVERAL,
            bank_match.SHARED,
            bank_match.NOT_BOOKED,
            "matched",
            "none",
        )
    }
    top = []
    if not lines:
        return reasons, top
    accounts = collections.OrderedDict()
    for line in lines:
        accounts.setdefault(line["bank_account"], []).append(line)
    decided = {}
    for account, account_lines in accounts.items():
        gl, company = frappe.db.get_value(
            "Bank Account", account, ["account", "company"]
        ) or (None, None)
        currency = (
            frappe.db.get_value("Account", gl, "account_currency") if gl else None
        )
        if not (currency and company):
            continue
        for line in account_lines:
            line["currency"] = currency
            line["company"] = company
        decided.update(
            {
                decision["line"]: decision
                for decision in bank_match.decide(
                    account_lines,
                    bank_match_job._open_documents(company, currency, user=user),
                    bank_match_job._open_payments(company, currency, gl, user=user),
                    bank_match_job._rules(company, user=user),
                )
            }
        )
    for line in lines:
        decision = decided.get(line["name"])
        if decision is None:
            reason = None
        elif decision["state"] == "match":
            reason = "matched"
            reasons[reason] += 1
        elif decision["state"] == "uncertain" and decision["reason"]:
            reason = decision["reason"]
            reasons[reason] += 1
        else:
            reason = "none"
            reasons["none"] += 1
        if len(top) < TOP:
            top.append(
                {
                    "doctype": "Bank Transaction",
                    "bank_account": line["bank_account"],
                    "name": line["name"],
                    "date": str(line["date"])[:10],
                    "amount": bank_match.signed(line),
                    "reference_number": _text(
                        line.get("reference_number"), FIELD_CHARS
                    ),
                    "end_to_end_id": _text(line.get("end_to_end_id"), FIELD_CHARS),
                    "reason": reason,
                }
            )
    return reasons, top


def _now_utc():
    """The clock both feeds write their last_sync with: naive UTC (wise.py, paypal.py do the same),
    not now_datetime() (the site's timezone, whatever System Settings say) - one stamp, one clock,
    or the staleness skews by the offset between them."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _hours_since(stamp, now=None):
    """Hours since a Datetime stamp, or None when there is none."""
    if not stamp:
        return None
    when = (
        stamp
        if isinstance(stamp, datetime.datetime)
        else datetime.datetime.fromisoformat(str(stamp)[:19])
    )
    return ((now or _now_utc()) - when).total_seconds() / 3600.0


def _upload_status(today):
    """The latest Bank Statement Upload with its month, and whether the last full month has none; when the doctype is
    not readable for the user, the month is not checked instead of failing the feed report.
    """
    first, last = _month_bounds(today)
    status = {
        "latest_upload": None,
        "upload_month": None,
        "upload_month_missing": None,
    }
    try:
        count = _count(
            "Bank Statement Upload",
            # between, not <=: imported_on is a Datetime field, and frappe extends between's upper
            # bound to the end of that day, while <= on a date stops at its midnight and would miss
            # an import from the month's last day
            [["imported_on", "between", [first, last]]],
        )
        rows = _get_list(
            "Bank Statement Upload",
            fields=["name", "imported_on"],
            order_by="imported_on desc",
            limit_page_length=1,
        )
    except NotPermitted:
        status["note"] = (
            "Bank Statement Upload is not readable for you, so its month is not checked"
        )
        return status
    status["upload_month_missing"] = count == 0
    if rows:
        status["latest_upload"] = rows[0]["name"]
        status["upload_month"] = str(rows[0]["imported_on"])[:7]
    return status


def _ledger_banner(settings, today=None):
    """The line every answer starts with while today is before the ledger date, else None.

    An empty setting counts as the default (2027-01-01), the date the bookkeeper's plan names.
    """
    raw = getattr(settings, "ledger_actions_from", None)
    if raw and not isinstance(raw, datetime.date):
        raw = datetime.date.fromisoformat(str(raw)[:10])
    ledger_from = raw or LEDGER_ACTIONS_FROM_DEFAULT
    if (today or frappe.utils.getdate()) >= ledger_from:
        return None
    return BANNER_TEXT.format(
        date=f"{ledger_from.day}.{ledger_from.month}.{ledger_from.year}"
    )


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
    """The past turns as the request carries them: the last HISTORY_MESSAGES messages, user and assistant only."""
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


def _items(items, data):
    """The answer's items as the page links them: known doctypes with existing documents, each once, at most TOP.

    The model's strings are cleaned and capped, the doctype and the document are both checked against the database,
    and the link is built here the way the desk spells routes: /app/<doctype slug>/<name>. What the model invents is
    dropped, not linked; the scan stops once TOP are collected, so a long answer cannot turn into many lookups.
    """
    doctype_by_collector = {
        "todos": "ToDo",
        "sales.overdue": "Sales Invoice",
        "bills.due": "Purchase Invoice",
        "vat.quarter": "VAT Declaration",
        "payroll.month": "Salary Slip",
        "expenses.approve": "Expense Claim",
        "month.revaluation": "Exchange Rate Revaluation",
    }
    allowed = set()
    for collector_result in data:
        if not isinstance(collector_result, dict):
            continue
        key = collector_result.get("key")
        for row in collector_result.get("top") or []:
            if not isinstance(row, dict):
                continue
            doctype = row.get("doctype") or doctype_by_collector.get(key)
            name = row.get("name")
            if doctype and name:
                allowed.add((doctype, name))
            if (
                key == "todos"
                and row.get("reference_type")
                and row.get("reference_name")
            ):
                allowed.add((row["reference_type"], row["reference_name"]))
            if key == "bank.lines" and row.get("bank_account"):
                allowed.add(("Bank Account", row["bank_account"]))

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
        if (
            (doctype, name) not in allowed
            or not known[doctype]
            or not frappe.db.exists(doctype, name)
            or not frappe.has_permission(
                doctype, "read", doc=name, user=frappe.session.user
            )
        ):
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
