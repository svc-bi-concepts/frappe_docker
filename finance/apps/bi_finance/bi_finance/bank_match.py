"""Which bank line pays which document, where it is certain; the rest stays for a person.

Pure Python, no site: the bank_match_job module reads ERPNext and acts on what decide() returns, and the tests run here.

A line is decided by the first pass that names exactly one document of the right direction, currency and amount:

1. reference: the line's reference (camt's creditor reference, Wise's reference number) is the SCOR creditor reference
   of an open Sales Invoice (reference.creditor_reference, the number printed on the slip).
2. payment reference: the line's EndToEndId (camt), else its reference or transaction id, is the reference_no of an
   unallocated Payment Entry, which is how a payment run names the payment it made (payment_run.py).
3. party: the description names a party (a word of its name in common) and exactly one open invoice or bill of that party
   has the amount, dated within ANCHOR_DAYS of the line. The party is the one the description names; no other link.
4. rule: no document is named, and the first Bank Transaction Rule (by priority) that matches books the line to its
   account. The rule's test mirrors ERPNext's evaluate_rule, so the same rule reads the same way in both.

Both sides count: a document that two lines claim is certain for neither, and both stay (ledgerdemain's rule for lines
that reconcile without a person: unique from both sides). A line that several documents fit stays too.
"""

import collections
import datetime
import re

from bi_finance.reference import creditor_reference

ANCHOR_DAYS = 5

# words that name no party: legal forms and articles; the party word must be a name word of three letters or more
PARTY_GENERIC = frozenset("ag gmbh sa sarl ltd llc inc und the der die von mit".split())

INVOICES = ("Sales Invoice", "Purchase Invoice")

PASSES = ("reference", "payment_reference", "party", "rule")

# why a line stays: a person decides it
SEVERAL = "several"
SHARED = "shared"
NOT_BOOKED = "not_booked"


def key(text):
    """A reference compared as its characters: upper case, no spaces ('RF18 5390' is 'RF185390')."""
    return re.sub(r"\s+", "", str(text or "")).upper()


def words(text):
    """The name words of a text: letters of three or more, without the legal forms and articles."""
    return {word for word in re.findall(r"[^\W\d_]{3,}", (text or "").lower()) if word not in PARTY_GENERIC}


def signed(line):
    """A line's amount, the deposit less the withdrawal, to the cent."""
    return round(float(line.get("deposit") or 0) - float(line.get("withdrawal") or 0), 2)


def _day(value):
    return datetime.date.fromisoformat(str(value)[:10])


def fits(line, doc):
    """Whether a document can be the one a line pays: same direction, currency and amount to the cent.

    Direction: a deposit pays what is owed to us (a Sales Invoice, a received Payment Entry), a withdrawal what we owe.
    """
    direction = "in" if signed(line) > 0 else "out"
    return (
        doc["direction"] == direction
        and doc["currency"] == line["currency"]
        and abs(doc["amount"] - abs(signed(line))) < 0.005
    )


def sales_invoice_reference(invoice_name):
    """The reference a Sales Invoice's slip carries, normalised for comparison; None when its number gives none."""
    try:
        return key(creditor_reference(invoice_name))
    except ValueError:
        return None


def by_reference(line, docs):
    """Pass 1: the open Sales Invoices whose creditor reference is the line's reference, and that fit the line."""
    reference = key(line.get("reference_number"))
    if not reference:
        return []
    return [doc for doc in docs if doc["doctype"] == "Sales Invoice" and doc.get("reference") == reference and fits(line, doc)]


def by_payment_reference(line, payments):
    """Pass 2: the unallocated Payment Entries that name the line. The line's EndToEndId (camt) is the payment run's own
    reference, so it is tried first; only when no Payment Entry of that id fits does the line's reference number or
    transaction id count."""
    end_to_end = {key(line.get("end_to_end_id"))} - {""}
    found = [payment for payment in payments if payment.get("reference") in end_to_end and fits(line, payment)]
    if found:
        return found
    names = {key(line.get("reference_number")), key(line.get("transaction_id"))} - {""}
    return [payment for payment in payments if payment.get("reference") in names and fits(line, payment)]


def by_party(line, docs):
    """Pass 3: the open invoices and bills whose party the description names, that fit the line and are within the anchor.

    The line's date and the invoice's date are compared, so a bill paid months later is left for a person.
    """
    described = words(line.get("description"))
    if not described:
        return []
    found = []
    for doc in docs:
        if doc["doctype"] not in INVOICES or not fits(line, doc):
            continue
        if not described & words(doc.get("party_name")):
            continue
        if abs((_day(line["date"]) - _day(doc["date"])).days) <= ANCHOR_DAYS:
            found.append(doc)
    return found


def rule_matches(line, rule):
    """Whether a Bank Transaction Rule takes a line. The same test as ERPNext's Bank Transaction Rule.evaluate_rule:
    the company, the transaction type, the amount limits (when set) and then any one description condition. A rule with
    no description condition never matches.
    """
    if rule.get("company") != line.get("company"):
        return False
    deposit, withdrawal = float(line.get("deposit") or 0), float(line.get("withdrawal") or 0)
    if rule.get("transaction_type") == "Withdrawal" and withdrawal == 0:
        return False
    if rule.get("transaction_type") == "Deposit" and deposit == 0:
        return False
    amount = withdrawal or deposit
    if rule.get("min_amount") and amount < rule["min_amount"]:
        return False
    if rule.get("max_amount") and amount > rule["max_amount"]:
        return False
    description = (line.get("description") or "").lower()
    for condition in rule.get("description_rules") or []:
        value = (condition.get("value") or "").lower()
        check = condition.get("check")
        if check == "Contains" and value in description:
            return True
        if check == "Starts With" and description.startswith(value):
            return True
        if check == "Ends With" and description.endswith(value):
            return True
        if check == "Regex" and re.search(value, description):
            return True
    return False


def rule_for(line, rules):
    """The first rule by priority that takes the line: (rule, None) to book it, (None, reason) when the first match is
    one this job does not book, (None, None) when no rule takes it."""
    for rule in sorted(rules, key=lambda r: (r.get("priority") is None, r.get("priority") or 0)):
        if not rule_matches(line, rule):
            continue
        if rule.get("classify_as") != "Bank Entry" or rule.get("bank_entry_type") not in (None, "", "Single Account") or not rule.get("account"):
            return None, "rule {} is not a single-account Bank Entry".format(rule.get("name"))
        return rule, None
    return None, None


def decide(lines, docs, payments, rules):
    """What each line becomes: a list of decisions, one per line, in the lines' order.

    A decision is {line, state, pass, target, reason, date}. state is "match" (pass and target name the document or the
    rule), "uncertain" (reason is SEVERAL, SHARED or NOT_BOOKED: a person decides it) or "none" (no pass takes it). lines, docs and payments are read, not changed.
    docs: open invoices and bills (doctype, name, party_name, direction, amount, currency, date, reference);
    payments: unallocated Payment Entries (doctype "Payment Entry", name, reference, direction, amount, currency, date).
    """
    named = {}
    for line in lines:
        found = [("reference", doc) for doc in by_reference(line, docs)]
        found += [("payment_reference", doc) for doc in by_payment_reference(line, payments)]
        if not found:
            found = [("party", doc) for doc in by_party(line, docs)]
        named[line["name"]] = found

    # both sides: a document that any line names as a candidate is certain for none of the lines that name it
    claims = collections.Counter((doc["doctype"], doc["name"]) for found in named.values() for _, doc in found)

    decisions = []
    for line in lines:
        found = named[line["name"]]
        if len(found) > 1:
            decisions.append(_decision(line, "uncertain", reason=SEVERAL))
        elif len(found) == 1:
            pass_name, doc = found[0]
            target = (doc["doctype"], doc["name"])
            if claims[target] > 1:
                decisions.append(_decision(line, "uncertain", reason=SHARED))
            else:
                decisions.append(_decision(line, "match", pass_name, target))
        else:
            rule, reason = rule_for(line, rules)
            if rule:
                decisions.append(_decision(line, "match", "rule", rule["name"]))
            elif reason:
                decisions.append(_decision(line, "uncertain", reason=NOT_BOOKED))
            else:
                decisions.append(_decision(line, "none"))
    return decisions


def _decision(line, state, pass_name=None, target=None, reason=None):
    return {
        "line": line["name"], "state": state, "pass": pass_name, "target": target, "reason": reason,
        "date": str(line["date"])[:10],
    }


def summary(decisions):
    """Counts only, for a note: per pass and state, and per pass and month. Names stay out of it."""
    by_state = collections.Counter((d["state"], d["pass"] or "-") for d in decisions)
    by_month = collections.Counter((str(d["date"])[:7], d["pass"] or "-") for d in decisions if d["state"] == "match")
    uncertain = collections.Counter(d["reason"] for d in decisions if d["state"] == "uncertain")
    return {
        "lines": len(decisions),
        "match": sum(n for (state, _), n in by_state.items() if state == "match"),
        "uncertain": sum(n for (state, _), n in by_state.items() if state == "uncertain"),
        "none": sum(n for (state, _), n in by_state.items() if state == "none"),
        "by_pass": {name: sum(n for (state, p), n in by_state.items() if state == "match" and p == name) for name in PASSES},
        "by_month": {"{} {}".format(month, name): n for (month, name), n in sorted(by_month.items())},
        "uncertain_reasons": dict(sorted(uncertain.items())),
    }
