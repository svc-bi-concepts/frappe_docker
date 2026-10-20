"""The desk assistant's four actions with a ledger effect (erp-9f7y, part 2a2), registered in assistant_actions.ACTIONS.

    match_bank_transaction      a bank line is the payment of one open invoice, bill or Payment Entry     level 1 or 2
    submit_revaluation          the month's Exchange Rate Revaluation draft is submitted                  level 1 or 2
    submit_payment_proposal     a draft Payment Proposal is submitted (its Payment Entries follow)        always 2
    submit_vat_declaration      a draft VAT Declaration is submitted                                      always 2

They call the mechanisms that exist (bank_match_job, payment_run through the doctype, vat_declaration, mwst_quarterly)
and book nothing of their own. Each type's hard checks and its check score are the table in erp-00z0: a failed structural
check (wrong currency, direction or amount; target not open; figures that differ from a fresh computation) refuses the
action, a duplicate or the ledger date caps the confidence at 0.5. Before the ledger date (assistant_actions) every one of
them applies as a dry run and returns the text of what it would do.

What stays the person's, never the assistant's: the bank file download and upload of a payment run, the transfer file and
the ESTV upload of a VAT declaration (assistant_actions.NEVER_ACTIONS). Reads are bounded by the one target: one line, one
document, the lines of the same account and amount with a LIMIT, never a table (mem-4v01).

The module registers itself on import, which assistant_actions does at its end: whoever imports the registry has the types.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

import frappe
from bi_finance import assistant_actions as base
from bi_finance import bank_match
from bi_finance.assistant_levels import ALWAYS_APPROVER, ONE_CLICK

MATCH_DOCTYPES = ("Sales Invoice", "Purchase Invoice", "Payment Entry")

# the other lines of the line's account with the line's amount that the shared check reads; more than this cannot be told
SHARED_LINES_LIMIT = 50

# the party pass at the job's anchor (bank_match.ANCHOR_DAYS, 5 days) scores 0.90, at this one 0.80
PARTY_WIDE_DAYS = 30

# the bexio figures of one quarter: a Ziffer each, 25 at most (mwst_quarterly.ROWS)
BEXIO_ROWS_LIMIT = 100

# the check score table of match_bank_transaction (erp-00z0): the strongest passed row is the score
SCORE_REFERENCE = 1.00
SCORE_PAYMENT_RUN = 0.95
SCORE_PARTY_NEAR = 0.90
SCORE_PARTY_WIDE = 0.80
SCORE_AMOUNT_ONLY = 0.60

# the pass name of a line a person (here: the assistant for one) matched; bank_match_job._book reads only "rule"
MANUAL_PASS = "manual"

# the first year of the ledger the assistant leads: the 2026 quarters are compared with bexio's figures
BEXIO_YEAR = "2026"

MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "bank_transaction": {
            "type": "string",
            "description": "The name of an unreconciled Bank Transaction.",
        },
        "doctype": {"type": "string", "enum": list(MATCH_DOCTYPES)},
        "name": {
            "type": "string",
            "description": "The open Sales Invoice or Purchase Invoice the line pays, or the unallocated Payment Entry it belongs to.",
        },
    },
    "required": ["bank_transaction", "doctype", "name"],
    "additionalProperties": False,
}

REVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "exchange_rate_revaluation": {
            "type": "string",
            "description": "The draft Exchange Rate Revaluation the monthly job made.",
        }
    },
    "required": ["exchange_rate_revaluation"],
    "additionalProperties": False,
}

PAYMENT_PROPOSAL_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "payment_proposal": {
            "type": "string",
            "description": "A draft Payment Proposal.",
        }
    },
    "required": ["payment_proposal"],
    "additionalProperties": False,
}

VAT_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "vat_declaration": {"type": "string", "description": "A draft VAT Declaration."}
    },
    "required": ["vat_declaration"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- shared helpers


def _cents(value):
    """A figure in whole rappen, so two figures are equal to the rappen or not (Decimal: no float noise)."""
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def _date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _readable(label, user, doctype, name):
    """The "can be read" row of base._can_read under a name that says which document it is."""
    return dict(base._can_read(user, doctype, name), check=label)


def _draft_row(docstatus, kind):
    return base._row(
        f"{kind} is a draft",
        docstatus == 0,
        hard=True,
        detail=(
            f"the {kind} is a draft"
            if docstatus == 0
            else f"the {kind} is not a draft; only a draft is submitted"
        ),
    )


# ---------------------------------------------------------------- match_bank_transaction


def _line(name):
    """The Bank Transaction as bank_match_job reads it, with the GL account of its bank account and that account's
    currency (the job sets the same); None when the line, or its bank account, is not there.
    """
    from bi_finance import bank_match_job

    fields = [*bank_match_job.LINE_FIELDS, "bank_account", "status", "docstatus"]
    found = frappe.db.get_value("Bank Transaction", name, fields)
    if not found:
        return None
    line = dict(zip(fields, found))
    account = (
        frappe.db.get_value(
            "Bank Account", line["bank_account"], ["account", "company"]
        )
        if line["bank_account"]
        else None
    )
    if not account:
        return None
    line["gl_account"], line["bank_company"] = account
    line["currency"] = frappe.db.get_value(
        "Account", line["gl_account"], "account_currency"
    )
    return line


def _match_target(line, payload, user):
    """The one document the line is said to pay, in the shape bank_match reads (None when it is not open for the line)."""
    from bi_finance import bank_match_job

    company, doctype, name = line["bank_company"], payload["doctype"], payload["name"]
    if doctype == "Payment Entry":
        found = bank_match_job._open_payments(
            company, None, line["gl_account"], user=user, name=name
        )
    else:
        found = [
            doc
            for doc in bank_match_job._open_documents(
                company, None, user=user, name=name
            )
            if doc["doctype"] == doctype
        ]
    return found[0] if found else None


def _other_lines_naming(line, doc):
    """How many other unreconciled lines of the line's account name the document: bank_match's own passes (reference,
    payment reference, party) on the lines with this amount, which is all that can name it (a pass needs the amount to
    fit). None when there are too many such lines to read."""
    from bi_finance import bank_match_job

    amount = abs(bank_match.signed(line))
    others = frappe.get_all(
        "Bank Transaction",
        filters={
            "bank_account": line["bank_account"],
            "docstatus": 1,
            "status": "Unreconciled",
            "name": ["!=", line["name"]],
        },
        or_filters=[["deposit", "=", amount], ["withdrawal", "=", amount]],
        fields=bank_match_job.LINE_FIELDS,
        limit_page_length=SHARED_LINES_LIMIT,
    )
    if len(others) >= SHARED_LINES_LIMIT:
        return None
    for other in others:
        other["currency"] = line["currency"]
    payment = doc["doctype"] == "Payment Entry"
    decisions = bank_match.decide(
        others, [] if payment else [doc], [doc] if payment else [], []
    )
    return sum(
        1
        for decision in decisions
        if decision["state"] == "match" or decision["reason"] == bank_match.SHARED
    )


def _check_match(payload, user, today):
    line_name, doctype, name = (
        payload["bank_transaction"],
        payload["doctype"],
        payload["name"],
    )
    rows = [
        _readable("line readable", user, "Bank Transaction", line_name),
        _readable("target readable", user, doctype, name),
    ]
    if not all(row["passed"] for row in rows):
        return rows
    line = _line(line_name)
    unreconciled = bool(
        line and line["docstatus"] == 1 and line["status"] == "Unreconciled"
    )
    rows.append(
        base._row(
            "line unreconciled",
            unreconciled,
            hard=True,
            detail=(
                "the bank line is unreconciled"
                if unreconciled
                else "the bank line is not an unreconciled line of a bank account"
            ),
        )
    )
    if not unreconciled:
        return rows
    doc = _match_target(line, payload, user)
    rows.append(
        base._row(
            "target open",
            doc is not None,
            hard=True,
            detail=(
                f"{doctype} is open"
                if doc
                else f"{doctype} is not open in the line's company (or, for a Payment Entry, not unallocated on its account)"
            ),
        )
    )
    if doc is None:
        return rows
    same_currency = doc["currency"] == line["currency"]
    right_direction = doc["direction"] == (
        "in" if bank_match.signed(line) > 0 else "out"
    )
    same_amount = abs(doc["amount"] - abs(bank_match.signed(line))) < 0.005
    rows += [
        base._row(
            "same currency",
            same_currency,
            hard=True,
            detail=(
                "the line and the target are in one currency"
                if same_currency
                else "the currencies differ"
            ),
        ),
        base._row(
            "same direction",
            right_direction,
            hard=True,
            detail=(
                "a deposit pays what is owed to us, a withdrawal what we owe"
                if right_direction
                else "the direction of the line does not fit the target"
            ),
        ),
        base._row(
            "amount equals the outstanding",
            same_amount,
            hard=True,
            detail=(
                "the line pays the whole outstanding amount"
                if same_amount
                else "the amount differs from the outstanding; partial payments are not matched"
            ),
        ),
    ]
    if not (same_currency and right_direction and same_amount):
        return rows
    naming = _other_lines_naming(line, doc)
    rows.append(
        base._row(
            "no other line names it",
            naming == 0,
            hard=True,
            cap=True,
            detail=(
                "no other unreconciled line names the target"
                if naming == 0
                else (
                    "another unreconciled line names the target; a person decides"
                    if naming
                    else f"over {SHARED_LINES_LIMIT} unreconciled lines have this amount; a person decides"
                )
            ),
        )
    )
    near = bool(bank_match.by_party(line, [doc]))
    wide = bool(bank_match.by_party(line, [doc], anchor_days=PARTY_WIDE_DAYS))
    rows += [
        base._row(
            "creditor reference",
            bool(bank_match.by_reference(line, [doc])),
            score=SCORE_REFERENCE,
            detail="the line's reference is the invoice's creditor reference",
        ),
        base._row(
            "payment run reference",
            bool(bank_match.by_payment_reference(line, [doc])),
            score=SCORE_PAYMENT_RUN,
            detail="the line's EndToEndId is the reference of a payment run's entry",
        ),
        base._row(
            "party within 5 days",
            near,
            score=SCORE_PARTY_NEAR,
            detail=f"the description names the party and the dates are within {bank_match.ANCHOR_DAYS} days",
        ),
        base._row(
            "party within 30 days",
            wide,
            score=SCORE_PARTY_WIDE,
            detail=f"the description names the party and the dates are within {PARTY_WIDE_DAYS} days",
        ),
        base._row(
            "amount only",
            bank_match.fits(line, doc),
            score=SCORE_AMOUNT_ONLY,
            detail="direction, currency and amount fit",
        ),
    ]
    return rows


def _match_amount(payload):
    line = frappe.db.get_value(
        "Bank Transaction", payload["bank_transaction"], ["deposit", "withdrawal"]
    )
    if not line:
        return 0.0
    return abs(bank_match.signed({"deposit": line[0], "withdrawal": line[1]}))


def _match_date(payload):
    return _date(
        frappe.db.get_value("Bank Transaction", payload["bank_transaction"], "date")
    )


def _entry_text(line, payload, amount):
    """What the booking would be, for the dry run: the Payment Entry's party, amount and accounts."""
    doctype, name = payload["doctype"], payload["name"]
    if doctype == "Payment Entry":
        party = frappe.db.get_value("Payment Entry", name, "party_name")
        return (
            f"Reconcile the bank line with the existing Payment Entry {name} of {party}: "
            f"{amount:.2f} {line['currency']}, dated {_date(line['date'])}."
        )
    receivable = doctype == "Sales Invoice"
    party, control = frappe.db.get_value(
        doctype,
        name,
        ["customer_name", "debit_to"] if receivable else ["supplier_name", "credit_to"],
    )
    source, target = (
        (control, line["gl_account"]) if receivable else (line["gl_account"], control)
    )
    return (
        f"Payment Entry ({'Receive' if receivable else 'Pay'}) for {party} against {doctype} {name}: "
        f"{amount:.2f} {line['currency']}, from {source} to {target}, dated {_date(line['date'])}; "
        "the bank line is reconciled with it."
    )


def _apply_match(payload, dry_run):
    from bi_finance import bank_match_job

    line = _line(payload["bank_transaction"])
    if line is None:
        raise ValueError("the bank line is not there")
    if dry_run:
        return _entry_text(line, payload, abs(bank_match.signed(line)))
    # the job's own booking, so a match by hand and the job's look the same: a Payment Entry, then the reconcile
    decision = {
        "pass": MANUAL_PASS,
        "state": "match",
        "target": (payload["doctype"], payload["name"]),
    }
    return "Payment Entry", bank_match_job._book(line, decision, line["gl_account"])


# ---------------------------------------------------------------- submit_revaluation


def _gain_loss_rows(rows):
    """The accounts with a gain or loss as (account, currency, rappen), sorted: two lists are the same revaluation when
    they are equal. A row without a gain or loss is no part of it, as ERPNext drops it on submit.
    """
    return sorted(
        (
            row.get("account") or "",
            row.get("account_currency") or "",
            _cents(row["gain_loss"]),
        )
        for row in rows
        if row.get("gain_loss")
    )


def _check_revaluation(payload, user, today):
    from bi_finance import bank_match_job

    name = payload["exchange_rate_revaluation"]
    rows = [_readable("target readable", user, "Exchange Rate Revaluation", name)]
    if not rows[0]["passed"]:
        return rows
    doc = frappe.get_doc("Exchange Rate Revaluation", name)
    posting = _date(doc.posting_date)
    rows.append(_draft_row(doc.docstatus, "revaluation"))
    month_end = bool(posting and (posting + datetime.timedelta(days=1)).day == 1)
    rows.append(
        base._row(
            "month end",
            month_end,
            hard=True,
            detail=(
                "the posting date is a month end"
                if month_end
                else "the monthly job dates its draft on a month end; this one is not"
            ),
        )
    )
    if doc.docstatus != 0 or not month_end:
        return rows
    drafted = _gain_loss_rows(doc.accounts)
    fresh = _gain_loss_rows(bank_match_job.revaluation_preview(doc.company, posting))
    rows.append(
        base._row(
            "equals a fresh preview",
            drafted == fresh,
            hard=True,
            detail=(
                "the draft equals revaluation_preview to the rappen"
                if drafted == fresh
                else "the draft differs from a fresh revaluation_preview; the rates or balances moved, or it was edited"
            ),
        )
    )
    if (posting.month, posting.day) == (12, 31):
        # the year-end revaluation takes the ESTV Jahresendkurs, entered as a Currency Exchange row dated 31 December
        currencies = sorted({currency for _, currency, _ in drafted})
        missing = [
            currency
            for currency in currencies
            if not base._first(
                "Currency Exchange", {"date": posting, "from_currency": currency}
            )
        ]
        rows.append(
            base._row(
                "year-end rates",
                not missing,
                hard=True,
                detail=(
                    "a Currency Exchange row dated 31 December exists per foreign currency"
                    if not missing
                    else f"no Currency Exchange row dated 31 December for {len(missing)} currencies: {', '.join(missing)}"
                ),
            )
        )
    return rows


def _revaluation_amount(payload):
    """The draft's total gain or loss, absolute: the sum of its rows' gain or loss, which is its total_gain_loss."""
    name = payload["exchange_rate_revaluation"]
    if not frappe.db.exists("Exchange Rate Revaluation", name):
        return 0.0
    doc = frappe.get_doc("Exchange Rate Revaluation", name)
    return abs(sum(_cents(row.get("gain_loss")) for row in doc.accounts)) / 100


def _revaluation_date(payload):
    return _date(
        frappe.db.get_value(
            "Exchange Rate Revaluation",
            payload["exchange_rate_revaluation"],
            "posting_date",
        )
    )


def _apply_revaluation(payload, dry_run):
    doc = frappe.get_doc(
        "Exchange Rate Revaluation", payload["exchange_rate_revaluation"]
    )
    if dry_run:
        accounts = sum(1 for row in doc.accounts if row.get("gain_loss"))
        return (
            f"Submit Exchange Rate Revaluation {doc.name} dated {_date(doc.posting_date)} "
            f"for {accounts} accounts."
        )
    doc.submit()
    return "Exchange Rate Revaluation", doc.name


# ---------------------------------------------------------------- submit_payment_proposal


def _iban_problems(doc):
    """The rows whose IBAN would stop the bank file: missing or failing the mod 97 check (payment_run.iban_is_valid), as
    erpnextswiss resolves it (an invoice's own, else the supplier's; a claim's employee's). An ESR row pays by its
    participation number, which erpnextswiss checks itself. Rows are named by their number, not by the party.
    """
    from bi_finance import payment_run

    problems = []
    debtor = frappe.db.get_value("Account", doc.pay_from_account, "iban")
    if debtor and not payment_run.iban_is_valid(debtor):
        problems.append("the pay-from account")
    for number, row in enumerate(doc.purchase_invoices, 1):
        if row.get("payment_type") == "ESR":
            continue
        iban = frappe.get_doc("Purchase Invoice", row.purchase_invoice).get(
            "iban"
        ) or frappe.db.get_value("Supplier", row.supplier, "iban")
        if not iban or not payment_run.iban_is_valid(iban):
            problems.append(f"bill row {number}")
    for number, row in enumerate(doc.expenses, 1):
        iban = frappe.db.get_value("Employee", row.employee, "bank_ac_no")
        if not iban or not payment_run.iban_is_valid(iban):
            problems.append(f"claim row {number}")
    return problems


def _unpaid_problems(doc, currency):
    """(rows already paid or not open, rows in another currency than the pay-from account): one read per table."""
    bills = [row.purchase_invoice for row in doc.purchase_invoices]
    claims = [row.expense_claim for row in doc.expenses]
    open_bills = {
        row["name"]: row["currency"]
        for row in (
            frappe.get_all(
                "Purchase Invoice",
                filters={
                    "name": ["in", bills],
                    "docstatus": 1,
                    "outstanding_amount": [">", 0],
                },
                fields=["name", "currency"],
                limit_page_length=len(bills),
            )
            if bills
            else []
        )
    }
    open_claims = {
        row["name"]: row["currency"]
        for row in (
            frappe.get_all(
                "Expense Claim",
                filters={"name": ["in", claims], "docstatus": 1, "status": "Unpaid"},
                fields=["name", "currency"],
                limit_page_length=len(claims),
            )
            if claims
            else []
        )
    }
    paid = [name for name in bills if name not in open_bills] + [
        name for name in claims if name not in open_claims
    ]
    other_currency = [
        name for name, ccy in {**open_bills, **open_claims}.items() if ccy != currency
    ]
    return paid, other_currency


def _check_payment_proposal_submit(payload, user, today):
    name = payload["payment_proposal"]
    rows = [_readable("target readable", user, "Payment Proposal", name)]
    if not rows[0]["passed"]:
        return rows
    doc = frappe.get_doc("Payment Proposal", name)
    rows.append(_draft_row(doc.docstatus, "proposal"))
    # payroll is never the assistant's (HR 7/8 need ledgerdemain's go per month), and a proposal with nothing to pay is none
    payable = bool(doc.purchase_invoices or doc.expenses) and not doc.salaries
    rows.append(
        base._row(
            "bills and claims only",
            payable,
            hard=True,
            detail=(
                "the rows are bills and expense claims"
                if payable
                else "the proposal has no bill or claim, or it has salary rows"
            ),
        )
    )
    account = (
        frappe.db.get_value("Account", doc.pay_from_account, "account_currency")
        if doc.pay_from_account
        else None
    )
    rows.append(
        base._row(
            "pay-from account",
            bool(account),
            hard=True,
            detail=(
                "a pay-from account is set"
                if account
                else "the pay-from account is missing"
            ),
        )
    )
    if doc.docstatus != 0 or not payable or not account:
        return rows
    problems = _iban_problems(doc)
    rows.append(
        base._row(
            "IBANs valid",
            not problems,
            hard=True,
            detail=(
                "every IBAN passes the check digits"
                if not problems
                else f"IBAN missing or invalid: {', '.join(problems[:5])}"
            ),
        )
    )
    paid, other_currency = _unpaid_problems(doc, account)
    rows += [
        base._row(
            "no bill already paid",
            not paid,
            hard=True,
            detail=(
                "every bill and claim is still open"
                if not paid
                else f"{len(paid)} rows are paid or not open"
            ),
        ),
        base._row(
            "currency of the account",
            not other_currency,
            hard=True,
            detail=(
                "every row is in the currency of the pay-from account"
                if not other_currency
                else f"{len(other_currency)} rows are in another currency; a payment run does not cover this"
            ),
        ),
    ]
    return rows


def _proposal_amount(payload):
    found = frappe.db.get_value(
        "Payment Proposal", payload["payment_proposal"], "total"
    )
    return abs(float(found or 0))


def _proposal_date(payload):
    return _date(
        frappe.db.get_value("Payment Proposal", payload["payment_proposal"], "date")
    )


def _apply_payment_proposal_submit(payload, dry_run):
    doc = frappe.get_doc("Payment Proposal", payload["payment_proposal"])
    if dry_run:
        return (
            f"Submit Payment Proposal {doc.name}: {len(doc.purchase_invoices)} bills and "
            f"{len(doc.expenses)} claims; its Payment Entries are made as drafts. "
            "The bank file stays yours."
        )
    doc.submit()  # payment_run.PaymentProposal.on_submit makes the Payment Entries
    return "Payment Proposal", doc.name


# ---------------------------------------------------------------- submit_vat_declaration


def _period_of(start, end):
    """'2026Q3' or '2026H1' for a declaration that covers exactly that period; None for any other dates."""
    from bi_finance import mwst_payment

    for part in ("Q1", "Q2", "Q3", "Q4", "H1", "H2"):
        period = f"{start.year}{part}"
        if mwst_payment.period_bounds(period) == (start, end):
            return period
    return None


def _bexio_differences(company, period, start, end):
    """The Ziffern of a 2026 quarter where MWST pro Quartal differs from bexio's, or None without bexio figures. The
    rows the report cannot compute yet (mwst_quarterly.OPEN_ROWS) are left out, as the report leaves them blank.
    """
    from bi_finance import mwst_quarterly, vat_declaration

    bexio = {
        row.ziffer: [Decimal(str(row.base or 0)), Decimal(str(row.tax or 0))]
        for row in frappe.get_all(
            "MWST bexio Ziffer",
            filters={"quarter": period},
            fields=["ziffer", "base", "tax"],
            limit_page_length=BEXIO_ROWS_LIMIT,
        )
    }
    if not bexio:
        return None
    figures = mwst_quarterly.figures_of(
        vat_declaration._payment_totals(company, start, end)
    )
    report = mwst_quarterly.ziffern_rows([period], {period: figures}, {period: bexio})
    return [
        row["ziffer"]
        for row in report
        if row["ziffer"] not in mwst_quarterly.OPEN_ROWS
        and row[f"{period} differenz"] not in (None, 0)
    ]


def _check_vat_submit(payload, user, today):
    from bi_finance import vat_declaration

    name = payload["vat_declaration"]
    rows = [_readable("target readable", user, "VAT Declaration", name)]
    if not rows[0]["passed"]:
        return rows
    doc = frappe.get_doc("VAT Declaration", name)
    rows.append(_draft_row(doc.docstatus, "declaration"))
    start, end = _date(doc.start_date), _date(doc.end_date)
    period = _period_of(start, end) if start and end else None
    rows.append(
        base._row(
            "period",
            bool(period),
            hard=True,
            detail=(
                f"the declaration covers {period}"
                if period
                else "the dates are no quarter or half year"
            ),
        )
    )
    if doc.docstatus != 0 or not period:
        return rows
    try:
        fresh = vat_declaration.fill_payment_basis(doc.company, period, dry_run=1)[
            "fields"
        ]
    except frappe.ValidationError as err:
        rows.append(base._row("equals a fresh fill", False, hard=True, detail=str(err)))
        return rows
    differing = sorted(
        field
        for field, value in fresh.items()
        if _cents(doc.get(field)) != _cents(value)
    )
    rows.append(
        base._row(
            "equals a fresh fill",
            not differing,
            hard=True,
            detail=(
                "the draft's figures equal a fresh fill_payment_basis dry run to the rappen"
                if not differing
                else f"{len(differing)} figures differ from a fresh fill: {', '.join(differing[:5])}"
            ),
        )
    )
    if period.startswith(BEXIO_YEAR) and period[4] == "Q":
        differences = _bexio_differences(doc.company, period, start, end)
        rows.append(
            base._row(
                "equals bexio",
                differences == [],
                hard=True,
                detail=(
                    f"MWST pro Quartal equals bexio's figures for {period}"
                    if differences == []
                    else (
                        f"bexio has no figures for {period} to compare"
                        if differences is None
                        else f"{len(differences)} Ziffern differ from bexio: {', '.join(differences[:8])}"
                    )
                ),
            )
        )
    return rows


def _vat_amount(payload):
    found = frappe.db.get_value(
        "VAT Declaration", payload["vat_declaration"], "payable_tax"
    )
    return abs(float(found or 0))


def _vat_date(payload):
    """The period end: the declaration is dated by the quarter it declares."""
    return _date(
        frappe.db.get_value("VAT Declaration", payload["vat_declaration"], "end_date")
    )


def _apply_vat_submit(payload, dry_run):
    doc = frappe.get_doc("VAT Declaration", payload["vat_declaration"])
    if dry_run:
        return (
            f"Submit VAT Declaration {doc.name} for {_date(doc.start_date)} to {_date(doc.end_date)}. "
            "The transfer file and the ESTV upload stay yours."
        )
    doc.submit()
    return "VAT Declaration", doc.name


# ---------------------------------------------------------------- the registry

base.register(
    base.Action(
        "match_bank_transaction",
        ONE_CLICK,
        MATCH_SCHEMA,
        _check_match,
        _apply_match,
        lambda p: ("Bank Transaction", p["bank_transaction"]),
        ledger_effect=True,
        amount=_match_amount,
        posting_date=_match_date,
    )
)
base.register(
    base.Action(
        "submit_revaluation",
        ONE_CLICK,
        REVALUATION_SCHEMA,
        _check_revaluation,
        _apply_revaluation,
        lambda p: ("Exchange Rate Revaluation", p["exchange_rate_revaluation"]),
        ledger_effect=True,
        amount=_revaluation_amount,
        posting_date=_revaluation_date,
    )
)
base.register(
    base.Action(
        "submit_payment_proposal",
        ALWAYS_APPROVER,
        PAYMENT_PROPOSAL_SUBMIT_SCHEMA,
        _check_payment_proposal_submit,
        _apply_payment_proposal_submit,
        lambda p: ("Payment Proposal", p["payment_proposal"]),
        ledger_effect=True,
        amount=_proposal_amount,
        posting_date=_proposal_date,
    )
)
base.register(
    base.Action(
        "submit_vat_declaration",
        ALWAYS_APPROVER,
        VAT_SUBMIT_SCHEMA,
        _check_vat_submit,
        _apply_vat_submit,
        lambda p: ("VAT Declaration", p["vat_declaration"]),
        ledger_effect=True,
        amount=_vat_amount,
        posting_date=_vat_date,
    )
)
