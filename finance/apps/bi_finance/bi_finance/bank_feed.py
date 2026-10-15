"""The bank feed shared by the bank APIs (Wise and PayPal): feed rows written as submitted Bank Transactions.

A row is the dict wise_client.feed_rows gives: transaction_id, date, deposit, withdrawal, currency, description,
reference_number. Its transaction_id is the external id. A Bank Transaction with that id is left alone on every later
run, so a second run writes nothing.

A period already imported by bexio has no transaction_id, so a row is matched to it by account, amount and date window
instead: a bexio line matches a feed row of the same amount whose date falls in the window from the row's date to the
line's date, later or equal (MATCH_WINDOW_DAYS, the authorisation day to the settlement day), once each; when several
pair, the nearest date wins. A card payment may settle within the FX spread of Wise's debit: it also matches within
FX_TOLERANCE when the two descriptions name a merchant word in common, else it stays unmatched and is listed. Each such
Bank Transaction takes one feed row, so the feed does not write a movement twice. The count matters: two movements of
the same amount near the same day are both kept, one line each. The reference is not compared, because bexio's lines
have none.

The matching is in match_new and new_rows, which read nothing, so they are tested without a database. plan and write are
the frappe side.

The rates the conversion to CHF needs are in load_rates: ERPNext's configured source (frankfurter.dev v2), asked once
per currency and range. fetch_rates, rate_days, carry_forward and fetch_start are pure and tested without a database.
"""

import collections
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import frappe
from frappe import _

RATE_URL = "https://api.frankfurter.dev/v2/rates"

RATE_USER_AGENT = "bi_finance-wise-feed/1.0"

RATE_TIMEOUT_SECONDS = 60

ONE_DAY = datetime.timedelta(days=1)

# the days a bexio line may fall after its feed row and still be the same movement: a card payment carries Wise's
# authorisation date and bexio's statement line its settlement date, a few days later
MATCH_WINDOW_DAYS = 4

# a card payment in a foreign currency may settle within this relative spread of Wise's debit (the FX spread), when the
# descriptions name the same merchant
FX_TOLERANCE = 0.01

# the words of a description that name no merchant: the generic card, fee and transfer words and the currency codes
GENERIC_WORDS = frozenset(
    "card transaction issued charges charge fee fees wise topped account sent money payment transfer deposit withdrawal "
    "chf eur usd gbp".split()
)


class RateError(Exception):
    """The rate source gave no usable answer. The message names the status or the error class, never the body."""


def match_new(rows, lines, window_days=MATCH_WINDOW_DAYS):
    """How feed rows pair with bexio lines: (create, matched, unmatched_lines). Pure, both lists are only read.

    rows: feed rows (date, deposit, withdrawal, description, kind). lines: the Bank Transactions that no feed wrote
    (bexio's: no transaction_id, no kind). A line matches a row of the same amount and direction whose date falls in the
    window from the row's date to the line's date, later or equal, window_days long, once each; when several pair, the
    nearest date wins, the other line stays. A CARD_PAYMENT row may also match within the FX tolerance of its amount
    when the two descriptions name a merchant word in common; otherwise it stays unmatched and is listed. matched is a
    list of (row, line, gap in days, within tolerance); the rows of no pair come back as create, the lines of no pair as
    unmatched_lines. Amounts are compared to the cent.
    """
    pairs = []
    for r_index, row in enumerate(rows):
        day = _day(row["date"])
        net = _net(row)
        for l_index, line in enumerate(lines):
            gap = (_day(line["date"]) - day).days
            if not 0 <= gap <= window_days:
                continue
            difference = abs(_net(line) - net)
            if difference <= 0.005:
                pairs.append((gap, 0, r_index, l_index))
            elif (
                row.get("kind") == "CARD_PAYMENT"
                and difference <= FX_TOLERANCE * abs(net) + 0.005
                and same_merchant(row.get("description"), line.get("description"))
            ):
                pairs.append((gap, 1, r_index, l_index))
    # nearest date first, the same amount before the tolerance, then the lists' order: the assignment is one to one
    pairs.sort()
    taken_rows, taken_lines = set(), set()
    matched = []
    for gap, tolerance, r_index, l_index in pairs:
        if r_index in taken_rows or l_index in taken_lines:
            continue
        taken_rows.add(r_index)
        taken_lines.add(l_index)
        matched.append((rows[r_index], lines[l_index], gap, bool(tolerance)))
    create = [row for index, row in enumerate(rows) if index not in taken_rows]
    unmatched = [line for index, line in enumerate(lines) if index not in taken_lines]
    return create, matched, unmatched


def new_rows(rows, fed_ids, lines, window_days=MATCH_WINDOW_DAYS):
    """The rows that are not yet in ERPNext: (create, summary). Pure: fed_ids and lines are read by the caller.

    fed_ids: the transaction_ids already on the account. lines: the account's Bank Transactions that no feed wrote
    (bexio's), as match_new takes them. A row whose transaction_id is fed is skipped; the rest are matched to a line once
    each (match_new) or created. The summary counts each rule apart, so a dry run shows how the rows were matched.
    """
    summary = {"rows": len(rows), "already_fed": 0}
    seen = set()
    unseen = []
    for row in rows:
        # a movement on the edge of two statement windows comes back in both; the first one counts, as write() would
        if row["transaction_id"] in seen:
            summary["rows"] -= 1
            continue
        seen.add(row["transaction_id"])
        if row["transaction_id"] in fed_ids:
            summary["already_fed"] += 1
            continue
        unseen.append(row)
    create, matched, _ = match_new(unseen, lines, window_days)
    summary["already_imported"] = len(matched)
    summary["matched_exact"] = sum(1 for _, _, _, tolerance in matched if not tolerance)
    summary["matched_fx"] = sum(1 for _, _, _, tolerance in matched if tolerance)
    summary["matched_by_gap"] = dict(
        sorted(collections.Counter(str(gap) for _, _, gap, _ in matched).items())
    )
    summary["create"] = len(create)
    return create, summary


def plan(bank_account, rows):
    """What a run would write to bank_account for these rows: (create, summary). Reads ERPNext, writes nothing.

    An account that does not exist yet has no transactions, so every row is new.
    """
    if not frappe.db.exists("Bank Account", bank_account):
        return rows, {
            "rows": len(rows),
            "already_fed": 0,
            "already_imported": 0,
            "matched_exact": 0,
            "matched_fx": 0,
            "matched_by_gap": {},
            "create": len(rows),
        }
    existing = frappe.get_all(
        "Bank Transaction",
        filters={"bank_account": bank_account, "docstatus": ["<", 2]},
        fields=["transaction_id", "date", "deposit", "withdrawal", "description"],
    )
    fed_ids = {r["transaction_id"] for r in existing if r["transaction_id"]}
    lines = [r for r in existing if not r["transaction_id"]]
    return new_rows(rows, fed_ids, lines)


def bank_account_name(prefix, currency, bank):
    """The name ERPNext gives a bank's Bank Account of a currency: "<prefix> <currency> - <bank>"."""
    return f"{prefix} {currency} - {bank}"


def open_bank_account(prefix, bank, currency, parent, company, numbers):
    """The Bank Account of a currency at a bank, with its GL account under parent, made when missing.

    The GL account is "<prefix> Kontokorrent" (with the currency after it unless CHF) and takes the first number in numbers
    that no account of the company has. An existing Bank Account is left as it is. Returns its name.
    """
    name = bank_account_name(prefix, currency, bank)
    if frappe.db.exists("Bank Account", name):
        return name
    if not frappe.db.exists("Bank", bank):
        frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert()
    gl_name = f"{prefix} Kontokorrent" + ("" if currency == "CHF" else " " + currency)
    account = frappe.get_doc(
        {
            "doctype": "Account",
            "account_number": _free_number(company, numbers),
            "account_name": gl_name,
            "parent_account": parent,
            "company": company,
            "account_currency": currency,
            "account_type": "Bank",
            "is_group": 0,
        }
    ).insert()
    frappe.get_doc(
        {
            "doctype": "Bank Account",
            "account_name": f"{prefix} {currency}",
            "bank": bank,
            "account": account.name,
            "company": company,
            "is_company_account": 1,
        }
    ).insert()
    return name


def _free_number(company, numbers):
    used = {
        str(n)
        for n in frappe.get_all(
            "Account", filters={"company": company}, pluck="account_number"
        )
        if n
    }
    for number in numbers:
        if str(number) not in used:
            return str(number)
    frappe.throw(
        _("No free account number in {0} to {1} for a new bank account.").format(
            numbers[0], numbers[-1]
        )
    )


def write(bank_account, rows):
    """Insert and submit the rows as Bank Transactions, as the bexio loader does (a submitted one posts no GL). Returns the count."""
    company = frappe.db.get_value("Bank Account", bank_account, "company")
    written = 0
    for row in rows:
        # checked again per row: the hourly job and a Sync now can run at once
        if frappe.db.exists(
            "Bank Transaction", {"transaction_id": row["transaction_id"]}
        ):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Bank Transaction",
                "company": company,
                "bank_account": bank_account,
                "date": row["date"],
                # Wise and PayPal give one date per row, so it is the booking and the value date too
                "booking_date": row["date"],
                "value_date": row["date"],
                "deposit": row["deposit"],
                "withdrawal": row["withdrawal"],
                "currency": row["currency"],
                "description": row["description"],
                "reference_number": row["reference_number"],
                "transaction_id": row["transaction_id"],
            }
        )
        doc.insert()
        doc.submit()
        written += 1
    return written


def _net(row):
    """A row's or a line's amount, signed, to the cent: the deposit less the withdrawal."""
    return round(float(row["deposit"] or 0) - float(row["withdrawal"] or 0), 2)


def _day(date):
    """A date or a string that starts with one, as a date: a feed row's 'YYYY-MM-DD' or a Bank Transaction's date."""
    return datetime.date.fromisoformat(str(date)[:10])


def merchant_words(text):
    """The words of a description that can name a merchant: alphabetic words of three letters or more, without the
    generic card, fee and transfer words and the currency codes."""
    return {
        word
        for word in re.findall(r"[a-z]+", (text or "").lower())
        if len(word) >= 3 and word not in GENERIC_WORDS
    }


def same_merchant(row_text, line_text):
    """Whether two descriptions name a merchant word in common. The FX tolerance trusts it; an exact amount needs it
    not, so a generic title never turns a near amount into a match."""
    words = merchant_words(row_text)
    return bool(words and words & merchant_words(line_text))


def load_rates(currency, start, end, dry_run, opener=urllib.request.urlopen):
    """The CHF rate of a currency for each day from start to end, as {(currency, day): rate} for the run's conversion.

    One request for the range, from the latest stored day or start, whichever is later. A live run stores each day it
    did not have as a Currency Exchange, named as ERPNext names them, so ERPNext's own lookup finds it later. A dry run
    stores nothing and converts with the fetched rates. A weekend or a holiday takes the latest earlier rate.
    """
    _check_source()
    stored = frappe.get_all(
        "Currency Exchange",
        filters={"from_currency": currency, "to_currency": "CHF"},
        pluck="date",
    )
    first = fetch_start(start, max(stored, default=None))
    if first > end:
        return {}
    days = fetch_rates(currency, first, end, opener=opener)
    if not dry_run:
        known = {str(day) for day in stored}
        for day, rate in days.items():
            if day not in known:
                _store_rate(currency, day, rate)
    return {
        (currency, day): rate for day, rate in carry_forward(days, first, end).items()
    }


def fetch_start(start, latest):
    """The first day to ask the source for: start, or the latest stored day when that is later (its row seeds the carry)."""
    if latest and latest > start:
        return latest
    return start


def fetch_rates(currency, start, end, opener=urllib.request.urlopen):
    """The CHF rate the source gives for each day from start to end: {day: rate}, days as YYYY-MM-DD. One request."""
    params = {"from": str(start), "to": str(end), "base": currency, "quotes": "CHF"}
    # the source answers 403 to urllib's default agent, so the request names itself
    headers = {"Accept": "application/json", "User-Agent": RATE_USER_AGENT}
    request = urllib.request.Request(
        RATE_URL + "?" + urllib.parse.urlencode(params), headers=headers, method="GET"
    )
    try:
        with opener(request, timeout=RATE_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        raise RateError(f"HTTP {err.code} from the rate source") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise RateError(
            f"no usable answer from the rate source ({type(err).__name__})"
        ) from None
    return rate_days(payload, currency)


def rate_days(payload, currency):
    """The CHF rates in the source's answer, a list of {date, base, quote, rate}: {day: rate}. Other pairs and zero rates are left out."""
    if not isinstance(payload, list):
        raise RateError("unexpected answer from the rate source")
    days = {}
    for item in payload:
        if not isinstance(item, dict):
            raise RateError("unexpected answer from the rate source")
        if (
            item.get("base") == currency
            and item.get("quote") == "CHF"
            and item.get("rate")
        ):
            days[item["date"]] = float(item["rate"])
    return days


def carry_forward(days, start, end):
    """A rate for each day from start to end: the day's own, or the latest earlier one. Days before the first rate have none.

    days: {day: rate}, days as YYYY-MM-DD. Returns {day: rate} for the days that have one.
    """
    out = {}
    last = None
    day = start
    while day <= end:
        last = days.get(str(day), last)
        if last is not None:
            out[str(day)] = last
        day += ONE_DAY
    return out


def _check_source():
    settings = frappe.get_single("Currency Exchange Settings")
    if settings.disabled or settings.service_provider != "frankfurter.dev - v2":
        frappe.throw(
            _(
                "Enable Currency Exchange Settings with frankfurter.dev - v2 first: the Wise rates come from it."
            )
        )


def _store_rate(currency, day, rate):
    """A Currency Exchange for buying and for selling, as ERPNext's own rates are stored."""
    try:
        frappe.get_doc(
            {
                "doctype": "Currency Exchange",
                "date": day,
                "from_currency": currency,
                "to_currency": "CHF",
                "exchange_rate": rate,
                "for_buying": 1,
                "for_selling": 1,
            }
        ).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # the hourly job and a Sync now can store the same day at once
        pass
