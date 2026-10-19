"""The desk assistant's actions: the registry, the checks and the AI Action audit log (erp-00z0, part 2a).

Part 1 (assistant.py) reads and answers. This module is what acts, and every path through it writes or updates one AI
Action row, the refused ones too, so the log is the whole story. No model call and no UI here: erp-wcf3 (2b) hands the
model the schemas below and calls propose(); erp-ss1j (2c) routes level 2 to the AI Action form. The ladder itself is
the pure function in assistant_levels.py.

    propose(user, task, action_type, payload, model_score)   level 0 applies at once, 1 and 2 wait as "proposed"
    approve(action, user, confirmed_amount)                   level 1: the requester; level 2: an AI Approver
    reject(action, user)
    undo(action, user)                                        level 0 types only

Each returns {"ok", "state", "message", "name"} and never throws on a policy refusal: a throw would roll back the
request and with it the row that says why. apply() runs in the calling user's own session (no frappe.set_user), and
the session user must be the user named, so a caller cannot approve as somebody else.

bexio leads until AI Assistant Settings.ledger_actions_from (default 2027-01-01): an action with a ledger effect whose
posting date is earlier can be proposed, levelled and approved, but apply() runs as a dry run and the row ends in
approved-dry-run. The ledger types (bank match, revaluation, payment proposal submit, VAT submit) come in erp-9f7y;
the ledger effect flag, the ledger date check and the dry run path here are generic and tested with a stub type.
"""

import datetime
import json
import urllib.parse

import frappe
from bi_finance.assistant_levels import (
    AUTO,
    AUTO_ACTIONS_PER_DAY,
    AUTO_THRESHOLD,
    LEDGER_ACTIONS_FROM_DEFAULT,
    NEVER,
    ONE_CLICK_AMOUNT_LIMIT,
    ONE_CLICK_THRESHOLD,
    check_score,
    compute_level,
    failed_hard,
    unit_score,
)

DOCTYPE = "AI Action"
APPROVER_ROLE = "AI Approver"
API_USER = "api-agent@finance.local"  # never approves: it is the assistant's own transport, not a person

SAVEPOINT = "ai_action_apply"
DUE_SOON_DAYS = (
    14  # the bills.due collector's horizon, the default date of a Payment Proposal
)
TEXT_CHARS = 500  # one free-text argument or stored error
DETAIL_CHARS = 140

# the states of an AI Action row
PROPOSED = "proposed"
AUTO_DONE = "auto-done"
APPROVED = "approved"
APPROVED_DRY_RUN = "approved-dry-run"
REJECTED = "rejected"
FAILED = "failed"
UNDONE = "undone"

# what the assistant never does, whatever the confidence: refused and logged. Any type not registered is refused
# the same way; the names here only give the reason a clearer text.
NEVER_ACTIONS = (
    "delete_doc",
    "cancel_doc",
    "amend_doc",
    "manage_users",
    "manage_roles",
    "manage_permissions",
    "change_settings",
    "download_bank_file",
    "upload_bank_file",
    "create_pain001",
    "upload_estv",
    "send_mail",
    "write_bexio",
    "run_backup",
    "run_migrate",
    "create_payroll_entry",
    "submit_payroll_entry",
    "create_salary_slip",
    "submit_salary_slip",
    "create_draft",
    "submit_draft",
)


class Action:
    """One registered action type.

    level_class: AUTO, ONE_CLICK or ALWAYS_APPROVER (assistant_levels). ledger_effect: it books something, so the
    ledger date applies; such a type is never AUTO. schema: the JSON schema of its arguments, which 2b hands the model.
    check(payload, user, today) -> rows; apply(payload, dry_run) -> (doctype, name), or the text of what it would do
    when dry_run; undo(doctype, name, payload) raises when it cannot (None: the type has no Undo). amount(payload)
    and posting_date(payload) feed the ladder and the ledger date; target(payload) is the document the row is about.
    """

    def __init__(
        self,
        name,
        level_class,
        schema,
        check,
        apply,
        target,
        undo=None,
        ledger_effect=False,
        amount=None,
        posting_date=None,
    ):
        if ledger_effect and level_class == AUTO:
            raise ValueError(f"{name}: an action with a ledger effect is never auto")
        if ledger_effect and posting_date is None:
            raise ValueError(
                f"{name}: an action with a ledger effect names its posting date"
            )
        self.name = name
        self.level_class = level_class
        self.ledger_effect = ledger_effect
        self.schema = schema
        self.check = check
        self.apply = apply
        self.undo = undo
        self.target = target
        self.amount = amount or (lambda payload: 0.0)
        self.posting_date = posting_date or (lambda payload: None)


ACTIONS = {}


def register(action):
    ACTIONS[action.name] = action
    return action


# ---------------------------------------------------------------- the entry points


def propose(user, task, action_type, payload, model_score):
    """Check, level and (level 0) apply one proposed action; the row it writes is named in the result."""
    settings = _settings()
    today = _today()
    payload = dict(payload) if isinstance(payload, dict) else {}
    doc = frappe.get_doc(
        {
            "doctype": DOCTYPE,
            "requested_by": user,
            "task": task,
            "action_type": action_type,
            "payload": json.dumps(payload, sort_keys=True, default=str),
            "model_score": unit_score(model_score),
            "state": PROPOSED,
            "level": NEVER,
            "checks": [],
        }
    )
    if user != frappe.session.user:
        return _refused_new(
            doc, "The action is proposed in the session of the user it is for."
        )
    spec = ACTIONS.get(action_type)
    if action_type in NEVER_ACTIONS or spec is None:
        reason = (
            "The assistant never does this."
            if action_type in NEVER_ACTIONS
            else "This is not an action the assistant knows."
        )
        return _refused_new(doc, reason)
    if not settings["actions_enabled"]:
        return _refused_new(doc, "Actions are switched off in AI Assistant Settings.")
    rows = _checks(spec, payload, user, today, settings)
    try:
        target_doctype, target_name = spec.target(payload)
        amount = spec.amount(payload)
    except (
        KeyError,
        TypeError,
    ):  # the arguments failed the schema; the ladder refuses them below
        target_doctype, target_name, amount = "", "", 0.0
    ladder = dict(settings, auto_actions_today=_auto_used_today(user, today))
    confidence, level = compute_level(spec, model_score, rows, amount, ladder)
    doc.update(
        {
            "target_doctype": target_doctype,
            "target_name": target_name,
            "amount": amount,
            "check_score": check_score(rows),
            "confidence": confidence,
            "level": str(level),
            "checks": rows,
        }
    )
    if level == NEVER:
        doc.state = REJECTED
        doc.error = _text("; ".join(row["detail"] for row in failed_hard(rows)))
        doc.insert(ignore_permissions=True)
        return _result(doc, False)
    doc.insert(ignore_permissions=True)
    if level == 0:
        _apply(doc, spec, payload, dry_run=False, done=AUTO_DONE)
        doc.save(ignore_permissions=True)
        return _result(doc, doc.state == AUTO_DONE)
    return _result(doc, True)


def approve(action, user, confirmed_amount=None):
    """Level 1: the user who asked. Level 2: an AI Approver, typing the amount; the requester only when no other
    enabled AI Approver exists. Applies, as a dry run for a ledger action before the ledger date.
    """
    doc = _load(action)
    settings = _settings()
    today = _today()
    if user != frappe.session.user:
        return _refused(doc, "Approve in your own session.")
    if doc.state != PROPOSED:
        return _refused(
            doc, f"The action is {doc.state}; only a proposed one is approved."
        )
    if not settings["actions_enabled"]:
        return _refused(doc, "Actions are switched off in AI Assistant Settings.")
    if user == API_USER:
        return _refused(doc, "The API user does not approve.")
    if str(doc.level) == "1":
        if user != doc.requested_by:
            return _refused(doc, "Only the user who asked approves a one-click action.")
    elif str(doc.level) == "2":
        if APPROVER_ROLE not in frappe.get_roles(user):
            return _refused(doc, f"Level 2 needs the role {APPROVER_ROLE}.")
        if user == doc.requested_by and _other_approver_exists(user):
            return _refused(doc, "Another AI Approver approves what you asked for.")
        if not _same_amount(confirmed_amount, doc.amount):
            return _refused(doc, "Type the amount of the action to confirm it.")
    else:
        return _refused(doc, f"A level {doc.level} action is not approved by hand.")
    spec = ACTIONS.get(doc.action_type)
    if spec is None:
        return _refused(doc, "This is not an action the assistant knows.")
    payload = frappe.parse_json(doc.payload)
    # the proposal may be days old: a structural check that fails now refuses it
    rows = _checks(spec, payload, doc.requested_by, today, settings)
    if any(not row.get("cap") for row in failed_hard(rows)):
        doc.state = REJECTED
        doc.error = _text("; ".join(row["detail"] for row in failed_hard(rows)))
        doc.save(ignore_permissions=True)
        return _result(doc, False)
    doc.approver = user
    doc.confirmed_amount = confirmed_amount
    posting = spec.posting_date(payload) if spec.ledger_effect else None
    dry_run = spec.ledger_effect and not (posting and posting >= _ledger_from(settings))
    _apply(doc, spec, payload, dry_run=dry_run, done=APPROVED)
    doc.save(ignore_permissions=True)
    return _result(doc, doc.state in (APPROVED, APPROVED_DRY_RUN))


def reject(action, user):
    """A proposed action turned down by the user who asked or by an AI Approver."""
    doc = _load(action)
    if user != frappe.session.user:
        return _refused(doc, "Reject in your own session.")
    if doc.state != PROPOSED:
        return _refused(
            doc, f"The action is {doc.state}; only a proposed one is rejected."
        )
    if not _may_manage(doc, user):
        return _refused(doc, "Only the user who asked or an AI Approver rejects.")
    doc.state = REJECTED
    doc.approver = user
    doc.error = f"Rejected by {user}."
    doc.save(ignore_permissions=True)
    return _result(doc, True)


def undo(action, user):
    """Takes back a level 0 action: it works while what the action made is still as it made it. Allowed with the kill
    switch off, as undoing only removes."""
    doc = _load(action)
    if user != frappe.session.user:
        return _refused(doc, "Undo in your own session.")
    if doc.state != AUTO_DONE:
        return _refused(
            doc, f"The action is {doc.state}; only an auto-done one is undone."
        )
    if not _may_manage(doc, user):
        return _refused(doc, "Only the user who asked or an AI Approver undoes.")
    spec = ACTIONS.get(doc.action_type)
    if spec is None or spec.undo is None:
        return _refused(doc, "This action has no Undo.")
    frappe.db.savepoint(SAVEPOINT)
    try:
        spec.undo(doc.result_doctype, doc.result_name, frappe.parse_json(doc.payload))
    except Exception as err:  # the reason is the row's, whatever the failure was
        frappe.db.rollback(save_point=SAVEPOINT)
        return _refused(doc, f"Undo failed: {_problem(err)}")
    doc.state = UNDONE
    doc.approver = user
    doc.save(ignore_permissions=True)
    return _result(doc, True)


# ---------------------------------------------------------------- the pieces


def _apply(doc, spec, payload, dry_run, done):
    """Runs the action in the session user's own session; a failure rolls back what it did and ends the row failed."""
    frappe.db.savepoint(SAVEPOINT)
    try:
        outcome = spec.apply(payload, dry_run)
    except Exception as err:  # the reason is the row's, whatever the failure was
        frappe.db.rollback(save_point=SAVEPOINT)
        doc.state = FAILED
        doc.error = _problem(err)
        return
    if dry_run:
        doc.state = APPROVED_DRY_RUN
        doc.dry_run_result = _text(outcome)
    else:
        doc.state = done
        doc.result_doctype, doc.result_name = outcome


def _checks(spec, payload, user, today, settings):
    """The rows of one proposal: the arguments against the schema, the type's own checks, the ledger date."""
    rows = []
    problems = _schema_problems(payload, spec.schema)
    rows.append(
        _row(
            "arguments",
            not problems,
            hard=True,
            detail="; ".join(problems) or "the arguments fit the schema",
        )
    )
    if problems:
        return rows
    try:
        rows += [_row(**row) for row in spec.check(payload, user, today)]
    except (
        Exception
    ) as err:  # a check that cannot run is a failed structural check, never a pass
        rows.append(_row("checks ran", False, hard=True, detail=_problem(err)))
        return rows
    if spec.ledger_effect:
        posting = spec.posting_date(payload)
        ledger_from = _ledger_from(settings)
        on_time = bool(posting and posting >= ledger_from)
        rows.append(
            _row(
                "ledger date",
                on_time,
                hard=True,
                cap=True,
                detail=(
                    f"on or after the ledger date {ledger_from}"
                    if on_time
                    else f"the posting date is before the ledger date {ledger_from}; it applies as a dry run"
                ),
            )
        )
    return rows


def _row(check, passed, hard=False, cap=False, score=None, detail=""):
    return {
        "check": check,
        "passed": 1 if passed else 0,
        "hard": 1 if hard else 0,
        "cap": 1 if cap else 0,
        "score": score,
        "detail": _text(detail, DETAIL_CHARS),
    }


def _schema_problems(payload, schema):
    """The arguments that do not fit a flat object schema: a missing required one, an unknown one, a wrong type."""
    problems = []
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if payload.get(key) in (None, ""):
            problems.append(f"{key} is missing")
    for key, value in payload.items():
        spec = properties.get(key)
        if spec is None:
            problems.append(f"{key} is not an argument")
        elif value is not None and not isinstance(value, str):
            problems.append(f"{key} must be text")
        elif "enum" in spec and value is not None and value not in spec["enum"]:
            problems.append(f"{key} must be one of {', '.join(spec['enum'])}")
    return problems


def _refused_new(doc, reason):
    """A proposal turned away before it was levelled: logged as rejected, level never."""
    doc.state = REJECTED
    doc.error = reason
    doc.insert(ignore_permissions=True)
    return _result(doc, False)


def _refused(doc, reason):
    """An approval, rejection or undo turned away: the row keeps its state and records why."""
    doc.error = _text(reason)
    doc.save(ignore_permissions=True)
    return _result(doc, False, message=reason)


def _result(doc, ok, message=None):
    return {
        "ok": ok,
        "state": doc.state,
        "message": message or doc.error or "",
        "name": doc.name,
    }


def _load(action):
    return frappe.get_doc(DOCTYPE, action) if isinstance(action, str) else action


def _same_amount(typed, amount):
    """Whether what the approver typed is the action's amount to the rappen; text that is no number is not."""
    try:
        return round(float(typed), 2) == round(float(amount or 0), 2)
    except (TypeError, ValueError):
        return False


def _may_manage(doc, user):
    return user == doc.requested_by or APPROVER_ROLE in frappe.get_roles(user)


def _other_approver_exists(requester):
    """Whether an enabled AI Approver other than the requester (and the API user) exists: one read, LIMIT 1."""
    names = frappe.get_all(
        "Has Role",
        filters={"role": APPROVER_ROLE, "parenttype": "User"},
        pluck="parent",
        limit_page_length=50,
    )
    others = [name for name in names if name not in (requester, API_USER)]
    return bool(
        others
        and frappe.get_all(
            "User",
            filters={"name": ["in", others], "enabled": 1},
            pluck="name",
            limit_page_length=1,
        )
    )


def _auto_used_today(user, today):
    """The user's level 0 actions since midnight: one count."""
    return frappe.db.count(
        DOCTYPE,
        {
            "requested_by": user,
            "level": "0",
            "state": ["in", [AUTO_DONE, UNDONE, FAILED]],
            "creation": [">=", today],
        },
    )


def _settings():
    """AI Assistant Settings as the ladder reads them, with the defaults where a field is empty."""
    doc = frappe.get_single("AI Assistant Settings")

    def read(field, default):
        value = getattr(doc, field, None)
        return default if value is None or value == "" else value

    return {
        "actions_enabled": bool(read("actions_enabled", 0)),
        "auto_level_enabled": bool(read("auto_level_enabled", 1)),
        "auto_threshold": float(read("auto_threshold", AUTO_THRESHOLD)),
        "one_click_threshold": float(read("one_click_threshold", ONE_CLICK_THRESHOLD)),
        "one_click_amount_limit": float(
            read("one_click_amount_limit", ONE_CLICK_AMOUNT_LIMIT)
        ),
        "auto_actions_per_day": int(read("auto_actions_per_day", AUTO_ACTIONS_PER_DAY)),
        "ledger_actions_from": read("ledger_actions_from", LEDGER_ACTIONS_FROM_DEFAULT),
    }


def _ledger_from(settings):
    value = settings["ledger_actions_from"]
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _today():
    return frappe.utils.getdate()


def _text(value, chars=TEXT_CHARS):
    value = " ".join(str(value or "").split())
    return value if len(value) <= chars else value[: chars - 1] + "…"


def _problem(err):
    return _text(f"{type(err).__name__}: {err}")


# ---------------------------------------------------------------- the level 0 actions

COMMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_doctype": {"type": "string"},
        "reference_name": {"type": "string"},
        "content": {"type": "string", "description": "The comment, plain text."},
    },
    "required": ["reference_doctype", "reference_name", "content"],
    "additionalProperties": False,
}

TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "date": {"type": "string", "description": "The due day, YYYY-MM-DD."},
        "priority": {"type": "string", "enum": ["Low", "Medium", "High"]},
        "reference_type": {"type": "string"},
        "reference_name": {"type": "string"},
    },
    "required": ["description"],
    "additionalProperties": False,
}

CLOSE_TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "todo": {"type": "string", "description": "The name of your own open ToDo."}
    },
    "required": ["todo"],
    "additionalProperties": False,
}

PAYMENT_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "date": {
            "type": "string",
            "description": "Bills due up to this day, YYYY-MM-DD; default in 14 days.",
        },
        "currency": {"type": "string"},
    },
    "required": [],
    "additionalProperties": False,
}

VAT_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "period": {
            "type": "string",
            "description": "The ended quarter, for example 2026Q3.",
        },
    },
    "required": ["company", "period"],
    "additionalProperties": False,
}


def _can_read(user, doctype, name):
    exists = bool(frappe.db.exists(doctype, name))
    allowed = exists and bool(
        frappe.has_permission(doctype, "read", doc=name, user=user)
    )
    return _row(
        "target readable",
        allowed,
        hard=True,
        detail=f"{doctype} {name} "
        + ("can be read" if allowed else "is not there or you may not read it"),
    )


def _check_comment(payload, user, today):
    content = payload["content"]
    return [
        _can_read(user, payload["reference_doctype"], payload["reference_name"]),
        _row(
            "comment size",
            len(content) <= TEXT_CHARS,
            hard=True,
            detail=f"{len(content)} characters, at most {TEXT_CHARS}",
        ),
    ]


def _apply_comment(payload, dry_run):
    if dry_run:
        return f"Comment on {payload['reference_doctype']} {payload['reference_name']}: {_text(payload['content'])}"
    target = frappe.get_doc(payload["reference_doctype"], payload["reference_name"])
    comment = target.add_comment("Comment", payload["content"])
    return "Comment", comment.name


def _undo_comment(doctype, name, payload):
    found = frappe.db.get_value(
        "Comment", name, ["comment_type", "reference_doctype", "reference_name"]
    )
    if found != ("Comment", payload["reference_doctype"], payload["reference_name"]):
        raise ValueError("the comment is not there as the action made it")
    frappe.delete_doc("Comment", name)


def _check_todo(payload, user, today):
    rows = [
        _row(
            "description size",
            len(payload["description"]) <= TEXT_CHARS,
            hard=True,
            detail=f"at most {TEXT_CHARS} characters",
        )
    ]
    if payload.get("date"):
        try:
            datetime.date.fromisoformat(payload["date"])
            valid = True
        except ValueError:
            valid = False
        rows.append(
            _row(
                "due day",
                valid,
                hard=True,
                detail="YYYY-MM-DD" if not valid else "a valid day",
            )
        )
    if payload.get("reference_type") or payload.get("reference_name"):
        rows.append(
            _can_read(
                user,
                payload.get("reference_type", ""),
                payload.get("reference_name", ""),
            )
        )
    return rows


def _apply_todo(payload, dry_run):
    if dry_run:
        return f"ToDo for {frappe.session.user}: {_text(payload['description'])}"
    todo = frappe.get_doc(
        {
            "doctype": "ToDo",
            "allocated_to": frappe.session.user,  # the assistant sets no task for anybody else
            "description": payload["description"],
            "date": payload.get("date"),
            "priority": payload.get("priority") or "Medium",
            "reference_type": payload.get("reference_type"),
            "reference_name": payload.get("reference_name"),
            "status": "Open",
        }
    )
    todo.insert()
    return "ToDo", todo.name


def _set_todo_status(name, expect, status):
    todo = frappe.get_doc("ToDo", name)
    if todo.status != expect:
        raise ValueError(f"the ToDo is {todo.status}, not {expect}")
    todo.status = status
    todo.save()


def _undo_todo(doctype, name, payload):
    _set_todo_status(name, "Open", "Cancelled")


def _check_close_todo(payload, user, today):
    found = frappe.db.get_value("ToDo", payload["todo"], ["status", "allocated_to"])
    return [
        _row(
            "own open ToDo",
            found == ("Open", user),
            hard=True,
            detail=(
                "the ToDo is open and yours"
                if found == ("Open", user)
                else "not an open ToDo of yours"
            ),
        ),
        _can_read(user, "ToDo", payload["todo"]),
    ]


def _apply_close_todo(payload, dry_run):
    if dry_run:
        return f"Close ToDo {payload['todo']}"
    _set_todo_status(payload["todo"], "Open", "Closed")
    return "ToDo", payload["todo"]


def _undo_close_todo(doctype, name, payload):
    _set_todo_status(name, "Closed", "Open")


def _company(payload):
    return payload.get("company") or frappe.defaults.get_global_default("company")


def _proposal_date(payload, today):
    return payload.get("date") or str(today + datetime.timedelta(days=DUE_SOON_DAYS))


def _first(doctype, filters):
    """Whether one row matches: LIMIT 1, never a table."""
    return bool(
        frappe.get_all(doctype, filters=filters, pluck="name", limit_page_length=1)
    )


def _check_payment_proposal(payload, user, today):
    until = _proposal_date(payload, today)
    due = _first(
        "Purchase Invoice",
        {"docstatus": 1, "outstanding_amount": [">", 0], "due_date": ["<=", until]},
    )
    if not due and frappe.db.table_exists("Expense Claim"):
        due = _first("Expense Claim", {"docstatus": 1, "status": "Unpaid"})
    open_draft = _first("Payment Proposal", {"docstatus": 0})
    return [
        _row(
            "something due",
            due,
            hard=True,
            detail=(
                f"bills or claims due up to {until}"
                if due
                else f"no bill or claim due up to {until}"
            ),
        ),
        _row(
            "no draft proposal",
            not open_draft,
            hard=True,
            cap=True,
            detail=(
                "another draft Payment Proposal exists; open it"
                if open_draft
                else "no other draft"
            ),
        ),
    ]


def _apply_payment_proposal(payload, dry_run):
    until = _proposal_date(payload, _today())
    if dry_run:
        return f"Draft Payment Proposal for bills due up to {until}"
    from bi_finance import payment_run  # imports erpnextswiss, which only the image has

    url = payment_run.create_payment_proposal(
        date=until, company=_company(payload), currency=payload.get("currency")
    )
    if not url:
        raise ValueError("there was nothing to put into a proposal")
    # the desk button returns the form's URL; the name is its last part
    return "Payment Proposal", urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])


def _delete_draft(doctype, name, payload):
    if frappe.db.get_value(doctype, name, "docstatus") != 0:
        raise ValueError(f"the {doctype} is no longer a draft")
    frappe.delete_doc(doctype, name)


def _vat_declarations(payload, docstatus):
    """The company's VAT Declarations of the payload's period in one docstatus: one row at most is read."""
    from bi_finance import mwst_payment  # the period's bounds

    start, end = mwst_payment.period_bounds(payload["period"])
    return _first(
        "VAT Declaration",
        {
            "company": _company(payload),
            "start_date": start,
            "end_date": end,
            "docstatus": docstatus,
        },
    )


def _check_vat_draft(payload, user, today):
    from bi_finance import mwst_payment, vat_declaration

    try:
        _, end = mwst_payment.period_bounds(payload["period"])
    except ValueError as err:
        return [_row("period", False, hard=True, detail=str(err))]
    submitted = _vat_declarations(payload, 1)
    drafted = _vat_declarations(payload, 0)
    rows = [
        _row("period ended", end < today, hard=True, detail=f"the period ends {end}"),
        _row(
            "not submitted",
            not submitted,
            hard=True,
            detail="a submitted declaration exists" if submitted else "none submitted",
        ),
        _row(
            "no draft",
            not drafted,
            hard=True,
            cap=True,
            detail="a draft exists; open it" if drafted else "no draft yet",
        ),
    ]
    try:
        vat_declaration.fill_payment_basis(
            _company(payload), payload["period"], dry_run=1
        )
        rows.append(_row("dry run", True, hard=True, detail="the fill computes"))
    except frappe.ValidationError as err:
        rows.append(_row("dry run", False, hard=True, detail=str(err)))
    return rows


def _apply_vat_draft(payload, dry_run):
    from bi_finance import vat_declaration

    filled = vat_declaration.fill_payment_basis(
        _company(payload), payload["period"], dry_run=1 if dry_run else 0
    )
    if dry_run:
        return json.dumps(filled["fields"], sort_keys=True)
    return "VAT Declaration", filled["name"]


register(
    Action(
        "add_comment",
        AUTO,
        COMMENT_SCHEMA,
        _check_comment,
        _apply_comment,
        lambda p: (p["reference_doctype"], p["reference_name"]),
        undo=_undo_comment,
    )
)
register(
    Action(
        "create_todo",
        AUTO,
        TODO_SCHEMA,
        _check_todo,
        _apply_todo,
        lambda p: (p.get("reference_type", ""), p.get("reference_name", "")),
        undo=_undo_todo,
    )
)
register(
    Action(
        "close_todo",
        AUTO,
        CLOSE_TODO_SCHEMA,
        _check_close_todo,
        _apply_close_todo,
        lambda p: ("ToDo", p["todo"]),
        undo=_undo_close_todo,
    )
)
register(
    Action(
        "create_payment_proposal",
        AUTO,
        PAYMENT_PROPOSAL_SCHEMA,
        _check_payment_proposal,
        _apply_payment_proposal,
        lambda p: ("Payment Proposal", ""),
        undo=lambda doctype, name, payload: _delete_draft(doctype, name, payload),
    )
)
register(
    Action(
        "create_vat_draft",
        AUTO,
        VAT_DRAFT_SCHEMA,
        _check_vat_draft,
        _apply_vat_draft,
        lambda p: ("VAT Declaration", p["period"]),
        undo=lambda doctype, name, payload: _delete_draft(doctype, name, payload),
    )
)
