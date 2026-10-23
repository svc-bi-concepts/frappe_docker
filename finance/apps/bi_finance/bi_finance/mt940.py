"""Reads a UBS MT940 account statement and returns it as camt.parse does: the same dict, with version and kind 'mt940'.

Pure Python, no frappe: camt_import.py calls it for a file that is not XML, and the rest of the import is the camt one.
UBS's e-banking offers MT940 and not camt.053 for the bank lines from 30.09.2026 on.

The file holds one statement of one account, in the SWIFT envelope {1:}{2:}{3:}{4: ... -} or as the bare text block of
the 'light' variant. Lines end CRLF or LF. The fields read: :20: (the statement), :25: (the account, an IBAN),
:60F:/:60M: (opening balance), :61: (one transaction), :86: (its text), :62F:/:62M: (closing balance).

:61: (SWIFT C.2): value date YYMMDD; entry date MMDD, optional, in the year of the value date unless that is more than
half a year away (a year end); mark C (money in), D (money out), RC (reversal of a credit: money out), RD (reversal of
a debit: money in); a funds code letter, optional; the amount with a decimal comma; N and the 3-character type code;
the customer reference up to '//'; the bank reference after it. The line after :61: is its supplementary details.
The booking date is the entry date when there is one, else the value date.

:86: the text. UBS's enriched form starts a part with a code and '?' (Z45?, Z59? ...): the code is dropped and the text
after it kept, and a line without a code goes on the part before it. Codes are read at the start of a line only. No code
is read as a counterparty: none of the codes seen in UBS files (A10, A90, Y1B, Y4A, Y8A, Y92, Z04, Z11, Z15, Z33, Z40,
Z44, Z45, Z46, Z59) is documented here, so bank_party_name and bank_party_iban stay None. The description is the text;
a :61: without text takes its supplementary line.

reference: the bank reference, the booking date and the amount, as 'ref-2026-10-01-1000.00'. The same text again in the
file gets '-2', '-3' ... in file order, as camt's NtryRef does: the same line read from an overlapping later file gets
the same reference, and two lines with one bank reference do not collide. A line without a bank reference (or NONREF)
gets camt's content hash.

end_to_end_id: the customer reference when it is not NONREF. Unproven against a real payment run (erp-mfr3).
Zero-amount lines are not imported: they count under skipped['zero'].
"""

import re
from datetime import date, timedelta
from decimal import Decimal

from bi_finance import camt

TAG = re.compile(r":(\d{2}[A-Z]?):")
BALANCE = re.compile(r"([CD])(\d{6})([A-Z]{3})(\d+(?:,\d*)?)")
LINE = re.compile(
    r"(\d{6})(\d{4})?(RC|RD|C|D)[A-Z]?(\d+(?:,\d*)?)N([A-Z0-9]{3})([^/]*)(?://(.*))?"
)
CODE = re.compile(r"[A-Z][0-9A-Z]{2}\?(.*)")
IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
HALF_YEAR = timedelta(days=183)


def parse(content):
    """The statement of an MT940 file, in camt.parse's shape."""
    fields = _fields(_text(content))
    if not fields or fields[0][0] != "20":
        raise camt.CamtError("not an MT940 file: no :20: statement at its start")
    statements = _values(fields, "20")
    if len(statements) != 1:
        raise camt.CamtError(
            f"{len(statements)} statements in the file; one account per file"
        )
    accounts = _values(fields, "25")
    if len(accounts) != 1:
        raise camt.CamtError("the file must name one account (:25:)")
    iban = _iban(accounts[0])

    opening = _values(fields, "60F") or _values(fields, "60M")
    if not opening:
        raise camt.CamtError("no opening balance (:60F:) in the file")
    balances = {"OPBD": _balance(opening[0])}
    closing = _values(fields, "62F") or _values(fields, "62M")
    if closing:
        balances["CLBD"] = _balance(closing[-1])
    currency = balances["OPBD"]["currency"]
    if any(balance["currency"] != currency for balance in balances.values()):
        raise camt.CamtError("the balances of the file are in two currencies")

    skipped = {"not booked": 0, "covered": 0, "zero": 0}
    transactions = []
    counts = {}
    for value, info in _entries(fields):
        tx, bank = _entry(value, info, currency)
        amount = tx["deposit"] + tx["withdrawal"]
        if amount == 0:
            skipped["zero"] += 1
            continue
        if bank:
            base = f"{bank}-{tx['booking_date']}-{amount:.2f}"
            counts[base] = counts.get(base, 0) + 1
            tx["reference"] = base if counts[base] == 1 else f"{base}-{counts[base]}"
        transactions.append(tx)
    camt._fill_references(transactions)

    return {
        "version": "mt940",
        "kind": "mt940",
        "iban": iban,
        "currency": currency,
        "balances": balances,
        "transactions": transactions,
        "skipped": skipped,
    }


def _decode(content):
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _text(content):
    """Block 4 of the SWIFT envelope, or the whole file for the bare text block of the 'light' variant."""
    text = _decode(content).lstrip("\ufeff \t\r\n")
    start = text.find("{4:")
    if start < 0:
        return text
    text = text[start + len("{4:") :]
    end = re.search(r"(?:^|\n)-\}", text)
    if "{4:" in text[end.end() :] if end else "{4:" in text:
        raise camt.CamtError("more than one message in the file; one account per file")
    return text[: end.start()] if end else text


def _fields(text):
    """(tag, value) for each tagged field; a line without a tag goes on the value before it."""
    fields = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.strip() == "-":  # the end of the text block of the bare variant
            continue
        tag = TAG.match(line)
        if tag:
            fields.append((tag.group(1), line[tag.end() :].strip()))
        elif fields and line.strip():
            name, value = fields[-1]
            fields[-1] = (name, value + "\n" + line.strip())
    return fields


def _values(fields, tag):
    return [value for name, value in fields if name == tag]


def _entries(fields):
    """Each :61: with the :86: that follows it (None when there is none)."""
    entries = []
    for tag, value in fields:
        if tag == "61":
            entries.append([value, None])
        elif tag == "86":
            if not entries or entries[-1][1] is not None:
                raise camt.CamtError(":86: without a :61: before it")
            entries[-1][1] = value
    return entries


def _iban(value):
    iban = "".join(value.split()).upper()
    rearranged = iban[4:] + iban[:4]
    if not IBAN_SHAPE.fullmatch(iban) or (
        int("".join(str(int(char, 36)) for char in rearranged)) % 97 != 1
    ):
        raise camt.CamtError(
            "the account on :25: is not an IBAN, so the file cannot be matched to a Bank Account"
        )
    return iban


def _balance(value):
    """A :60F:/:62F: line (C or D, YYMMDD, currency, amount) as a balance: the amount signed, a debit negative."""
    match = BALANCE.fullmatch(value)
    if not match:
        raise camt.CamtError("a balance line (:60F: or :62F:) is not readable")
    sign, day, currency, amount = match.groups()
    amount = _amount(amount)
    return {
        "date": _day(day).isoformat(),
        "amount": -amount if sign == "D" else amount,
        "currency": currency,
    }


def _entry(value, info, currency):
    """One :61: and its :86:: the transaction (its amounts are zero for a zero line) and its bank reference."""
    head, _, supplementary = value.partition("\n")
    match = LINE.fullmatch(head)
    if not match:
        raise camt.CamtError(":61: line is not readable")
    value_text, entry_text, mark, amount_text, _, customer, bank = match.groups()
    value_day = _day(value_text)
    booking = _entry_day(value_day, entry_text) if entry_text else value_day
    amount = _amount(amount_text)
    money_in = mark in ("C", "RD")
    customer = (customer or "").strip() or None
    bank = (bank or "").strip() or None
    text = _description(info) or supplementary.strip()
    return (
        {
            "reference": None,
            "booking_date": booking.isoformat(),
            "value_date": value_day.isoformat(),
            "currency": currency,
            "deposit": amount if money_in else Decimal("0"),
            "withdrawal": Decimal("0") if money_in else amount,
            "end_to_end_id": None if customer in (None, "NONREF") else customer,
            "bank_party_name": None,
            "bank_party_iban": None,
            "reference_number": None,
            "description": text[: camt.DESCRIPTION_LENGTH] or None,
        },
        None if bank in (None, "NONREF") else bank,
    )


def _description(info):
    """The :86: text: each line's part after its code, or the line as it is when it has none."""
    parts = []
    for line in info.split("\n") if info else []:
        code = CODE.match(line)
        parts.append(code.group(1) if code else line)
    return " ".join(part.strip() for part in parts if part.strip())


def _entry_day(value, mmdd):
    """The entry date: MMDD in the year of the value date, moved a year when that is more than half a year away."""
    try:
        day = date(value.year, int(mmdd[:2]), int(mmdd[2:]))
        if day - value > HALF_YEAR:
            day = date(value.year - 1, day.month, day.day)
        elif value - day > HALF_YEAR:
            day = date(value.year + 1, day.month, day.day)
    except ValueError:
        raise camt.CamtError("an entry date (MMDD) of :61: is not a date")
    return day


def _day(text):
    try:
        return date(2000 + int(text[:2]), int(text[2:4]), int(text[4:]))
    except ValueError:
        raise camt.CamtError("a date (YYMMDD) is not a date")


def _amount(text):
    return Decimal(text.replace(",", "."))
