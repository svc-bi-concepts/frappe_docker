"""Book one Wise card payment that bexio never booked, from the merchant's earlier bookings, and reconcile it.

The Bank Transaction is a card payment on the Wise CHF account that bexio left unreconciled, so the GL has no pair for
it. Its merchant is named in the description ('Card transaction of <amount> <currency> ...'). The other Bank Transactions
of the account that name the same merchant are looked at: the GL rows of their reconciled vouchers give the expense
account and whether VAT was taken. When the history names exactly one expense account, one Journal Entry is booked on the
line's date, keyed bexio_id book-<name>, submitted, and the line is reconciled against it. Zero or several accounts book
nothing: the line goes on the short list for Benchi, with the counts.

Run it as:

    python3 finance/bexio/book_bank_line.py <Bank Transaction name> --dry-run
    python3 finance/bexio/book_bank_line.py <Bank Transaction name> --apply
    python3 finance/bexio/book_bank_line.py <Bank Transaction name> --account <number> --reason "<why>" --dry-run

--dry-run reads ERPNext and writes nothing there. The merchant words, the account, the VAT treatment and the planned
Journal Entry rows go to a private file (--plan); only counts and account numbers are printed. --apply writes one Journal
Entry and one reconciliation. A rerun finds the Journal Entry and the Reconciled line and does nothing. Standard library
only, plus import_master and import_bank.

--account books the line to the named account, no VAT, when the history has no earlier line of the merchant at all; with
any earlier line it is refused. Benchi chooses the account, and the reason goes into the Journal Entry's remark.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
from decimal import Decimal

import import_bank as ib
import import_master as im

WISE_BEXIO_ID = ib.WISE_BEXIO_ID
TAX_TYPE = "Tax"
VOUCHER_TYPES = ("Journal Entry", "Payment Entry")
KEY = "book-{}"
RECONCILE = "erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool.reconcile_vouchers"
RATE_STEP = Decimal("0.001")
CHOSEN_ON = "2026-10-11"

CARD = re.compile(r"card transaction of\s+\S+\s+[A-Z]{3}\s+(.+)$", re.IGNORECASE)
REFERENCE = re.compile(r"\s+ref(?:erence)?\b.*$", re.IGNORECASE)
TRAILING_DATE = re.compile(r"\s*\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\s*$")


class Refused(Exception):
    """The line cannot be booked by this command; the message says why, with counts and never a name."""

    def __init__(self, reason, counts=None):
        super().__init__(reason)
        self.counts = counts or {}


def merchant_words(description):
    """The merchant's words from a card line: the text after 'Card transaction of <amount> <currency>', less a trailing
    reference and date. None when the description is not a card line."""
    found = CARD.search(description or "")
    if not found:
        return None
    text = TRAILING_DATE.sub("", REFERENCE.sub("", found.group(1)))
    return re.findall(r"\w+", text.lower()) or None


def names_merchant(description, words):
    """Whether the merchant's words appear in the description, in order and as whole words."""
    text = " " + " ".join(re.findall(r"\w+", (description or "").lower())) + " "
    return " " + " ".join(words) + " " in text


def split_voucher(rows, bank_gl, tax_accounts):
    """One voucher's GL rows as (expense, tax): net debit per expense account and per tax account, the bank's row left out."""
    expense, tax = collections.Counter(), collections.Counter()
    for account, amount in rows:
        if account == bank_gl:
            continue
        (tax if account in tax_accounts else expense)[account] += amount
    return expense, tax


def treatment(history, bank_gl, tax_accounts):
    """The expense account and VAT treatment the history books, or the reason it books nothing.

    history: one entry per earlier line with a reconciled voucher, as {'vouchers': [rows]} with rows (account, amount).
    Returns (account, tax_account, rate) on one expense account with one VAT treatment (rate None: no VAT), else raises.
    """
    accounts, treatments = set(), set()
    for entry in history:
        for rows in entry["vouchers"]:
            expense, tax = split_voucher(rows, bank_gl, tax_accounts)
            accounts.update(expense)
            net = sum(expense.values(), Decimal("0"))
            if not tax:
                treatments.add((None, None))
                continue
            if len(tax) != 1 or net == 0:
                raise Refused(
                    f"a booking with VAT on {len(tax)} tax accounts or no expense"
                )
            ((tax_account, amount),) = tax.items()
            treatments.add((tax_account, (amount / net).quantize(RATE_STEP)))
    if len(accounts) != 1:
        raise Refused(
            "{} distinct expense accounts in the history ({}), want one".format(
                len(accounts),
                ", ".join(sorted(ib.account_number(a) for a in accounts)) or "none",
            )
        )
    if len(treatments) != 1:
        raise Refused(f"{len(treatments)} VAT treatments in the history, want one")
    ((tax_account, rate),) = treatments
    return next(iter(accounts)), tax_account, rate


def journal_rows(gross, plan, bank_gl):
    """The Journal Entry rows: the expense (net) and VAT debit, the bank credit for the gross amount."""
    rows = [
        {
            "account": plan["account"],
            "debit": float(plan["net"]),
            "debit_in_account_currency": float(plan["net"]),
        }
    ]
    if plan["tax_account"]:
        rows.append(
            {
                "account": plan["tax_account"],
                "debit": float(plan["tax"]),
                "debit_in_account_currency": float(plan["tax"]),
            }
        )
    rows.append(
        {
            "account": bank_gl,
            "credit": float(gross),
            "credit_in_account_currency": float(gross),
        }
    )
    return rows


def plan_for(gross, account, tax_account, rate):
    """Split the gross amount into net and VAT as the history's rate does: the bank line is always gross."""
    if rate is None:
        return {
            "account": account,
            "tax_account": None,
            "rate": None,
            "net": gross,
            "tax": Decimal("0"),
        }
    tax = ib.cents(gross * rate / (1 + rate))
    return {
        "account": account,
        "tax_account": tax_account,
        "rate": rate,
        "net": gross - tax,
        "tax": tax,
    }


def journal_entry(line, bank_gl, plan):
    """The Journal Entry dict for the line: company, date, remark, bexio key and rows, ready to insert. A plan with a reason
    is Benchi's choice of account, and the remark says so."""
    remark = "Wise card payment booked from history, line {}".format(line["name"])
    if plan.get("reason"):
        remark = "Wise card payment, account chosen by Benchi {}: {}, line {}".format(
            CHOSEN_ON, plan["reason"], line["name"]
        )
    return {
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "company": im.COMPANY,
        "posting_date": line["date"],
        "user_remark": remark,
        "bexio_id": KEY.format(line["name"]),
        "accounts": journal_rows(ib.cents(line["withdrawal"]), plan, bank_gl),
    }


def history_of(erp, line, account):
    """The earlier submitted Bank Transactions of the account that name the merchant: (count, [entry]) where each entry carries the
    GL rows of its reconciled vouchers, by voucher. Lines without a reconciled voucher count, but give no entry; a cancelled
    line is no booking and does not count."""
    words = merchant_words(line["description"])
    others = erp.list(
        "Bank Transaction",
        [["bank_account", "=", account], ["name", "!=", line["name"]]],
        ["name", "description", "status", "docstatus"],
    )
    named = [
        o
        for o in others
        if o["docstatus"] == 1 and names_merchant(o["description"], words)
    ]
    allocations = {
        o["name"]: erp.get("Bank Transaction", o["name"]).get("payment_entries") or []
        for o in named
    }
    vouchers = [
        (p["payment_document"], p["payment_entry"])
        for rows in allocations.values()
        for p in rows
    ]
    gl = collections.defaultdict(list)
    if vouchers:
        for row in erp.list(
            "GL Entry",
            [
                ["voucher_no", "in", sorted({v for _, v in vouchers})],
                ["is_cancelled", "=", 0],
                ["voucher_type", "in", list(VOUCHER_TYPES)],
            ],
            ["voucher_type", "voucher_no", "account", "debit", "credit"],
        ):
            gl[(row["voucher_type"], row["voucher_no"])].append(
                (row["account"], ib.cents(row["debit"]) - ib.cents(row["credit"]))
            )
    history = [
        {
            "vouchers": [
                gl[(p["payment_document"], p["payment_entry"])]
                for p in allocations[o["name"]]
            ]
        }
        for o in named
        if allocations[o["name"]]
    ]
    return len(named), history


def account_named(erp, number):
    """The GL account of the company whose name starts with the number ('6641 - Name - X'); exactly one, else refused."""
    if not number.isdigit():
        raise Refused(f"{number!r} is not an account number")
    found = erp.list(
        "Account",
        [["company", "=", im.COMPANY], ["name", "like", f"{number} - %"]],
        ["name"],
    )
    if len(found) != 1:
        raise Refused(f"{len(found)} accounts named {number}, want one")
    return found[0]["name"]


def decide(erp, line, bank_gl, override=None):
    """The plan for the line, or a Refused with the reason; counts are returned with it either way.

    override: (account number, reason) from Benchi's choice. It books the line with no VAT and only when the history has no
    earlier line of the merchant; with any earlier line the history decides and the override is refused.
    """
    if merchant_words(line["description"]) is None:
        raise Refused("the description is not a card transaction")
    account = line["bank_account"]
    earlier, history = history_of(erp, line, account)
    counts = {"earlier lines": earlier, "with a reconciled voucher": len(history)}
    if override:
        number, reason = override
        if earlier:
            raise Refused(
                "the --account override is only for a line whose merchant has no earlier line",
                counts,
            )
        plan = plan_for(
            ib.cents(line["withdrawal"]), account_named(erp, number), None, None
        )
        plan["reason"] = reason
        return plan, counts
    tax_accounts = {
        a["name"]
        for a in erp.list(
            "Account",
            [["company", "=", im.COMPANY], ["account_type", "=", TAX_TYPE]],
            ["name"],
        )
    }
    try:
        expense, tax_account, rate = treatment(history, bank_gl, tax_accounts)
    except Refused as err:
        raise Refused(str(err), counts)
    return plan_for(ib.cents(line["withdrawal"]), expense, tax_account, rate), counts


def bank_account_of(erp):
    """The Wise CHF Bank Account (bexio_id 2) and its GL account, the one a card line is credited to."""
    accounts = erp.list(
        "Bank Account", [["bexio_id", "=", WISE_BEXIO_ID]], ["name", "account"]
    )
    if len(accounts) != 1:
        raise Refused(f"{len(accounts)} Bank Accounts with the Wise bexio_id, want one")
    return accounts[0]["name"], accounts[0]["account"]


def book(erp, name, apply, override=None):
    """Plan the line and, with apply, book and reconcile it. Returns (lines to print, the plan or None).

    A line already Reconciled, or a submitted Journal Entry with its key, is left as it is. A draft Journal Entry with the key
    (an earlier run stopped before the submit) is submitted; a line whose Journal Entry exists is only reconciled.
    override: see decide.
    """
    line = erp.get("Bank Transaction", name)
    if line["docstatus"] != 1:
        raise Refused("the Bank Transaction is not submitted")
    if not line["withdrawal"] or line["deposit"]:
        raise Refused("the line is not a withdrawal")
    account, bank_gl = bank_account_of(erp)
    if line["bank_account"] != account:
        raise Refused("the line is on another bank account than the Wise CHF one")
    if line["status"] == "Reconciled":
        return [f"{name}: already Reconciled, nothing to do"], None
    existing = erp.list(
        "Journal Entry",
        [["bexio_id", "=", KEY.format(name)], ["docstatus", "!=", 2]],
        ["name", "docstatus"],
    )
    if existing:
        plan = None
        lines = [
            "{}: a Journal Entry with its key exists ({}), reconciling it".format(
                name, existing[0]["docstatus"]
            )
        ]
    else:
        plan, counts = decide(erp, line, bank_gl, override)
        plan["bank_gl"] = bank_gl
        lines = [
            "{}: {}".format(name, ", ".join(f"{v} {k}" for k, v in counts.items()))
        ]
        lines.append(
            "{}: account {}, VAT {}".format(
                name,
                ib.account_number(plan["account"]),
                "none" if plan["rate"] is None else "at {}".format(plan["rate"]),
            )
        )
        if plan.get("reason"):
            lines.append(
                "{}: account chosen by Benchi {}: {}".format(
                    name, CHOSEN_ON, plan["reason"]
                )
            )
    if not apply:
        lines.append("dry run: nothing was written to ERPNext")
        return lines, plan
    if existing:
        journal = existing[0]["name"]
        if existing[0]["docstatus"] != 1:
            journal = erp.call(
                "frappe.client.submit", doc=erp.get("Journal Entry", journal)
            )["name"]
    else:
        doc = erp.insert("Journal Entry", journal_entry(line, bank_gl, plan))
        journal = erp.call("frappe.client.submit", doc=doc)["name"]
    erp.call(
        RECONCILE,
        bank_transaction_name=name,
        vouchers=json.dumps(
            [{"payment_doctype": "Journal Entry", "payment_name": journal}]
        ),
    )
    status = erp.get("Bank Transaction", name)["status"]
    if status != "Reconciled":
        raise Refused(f"status is {status} after the allocation, not Reconciled")
    lines.append(f"{name}: booked as {journal} and reconciled")
    return lines, plan


def main(argv):
    parser = argparse.ArgumentParser(
        description="Book one unbooked Wise card line from the merchant's history and reconcile it."
    )
    parser.add_argument(
        "name", help="the Bank Transaction name, e.g. ACC-BTN-2026-00597"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="read ERPNext, write nothing, print the counts and the plan's account",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="book one Journal Entry and reconcile the line against it",
    )
    parser.add_argument(
        "--account",
        metavar="NUMBER",
        help="book to this account (e.g. 6641) chosen by Benchi, when the history has no earlier line",
    )
    parser.add_argument(
        "--reason",
        help="why Benchi chose the account, for the Journal Entry's remark (required with --account)",
    )
    parser.add_argument(
        "--plan",
        default=os.path.join(
            im.PRIVATE, f"book-bank-line-{datetime.date.today().isoformat()}.txt"
        ),
        help="private file for the proposal and the planned rows (never the repository)",
    )
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if bool(args.account) != bool(args.reason):
        parser.error("--account and --reason go together")
    override = (args.account, args.reason) if args.account else None
    erp = im.Erp.from_file(args.token_file)
    try:
        lines, plan = book(erp, args.name, args.apply, override)
    except im.ErpError as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 2
    except Refused as err:
        counts = ", ".join(f"{v} {k}" for k, v in err.counts.items())
        print(
            "{}: needs Benchi: {}{}".format(
                args.name, err, f" ({counts})" if counts else ""
            )
        )
        return 1
    if plan is not None:
        ib.write_private(
            args.plan,
            "\n".join(
                [
                    f"Book {args.name}: plan from the merchant's history",
                    "account: {}".format(plan["account"]),
                    "VAT account: {}, rate {}".format(
                        plan["tax_account"], plan["rate"]
                    ),
                    "rows: {}".format(
                        json.dumps(
                            journal_rows(
                                plan["net"] + plan["tax"], plan, plan["bank_gl"]
                            )
                        )
                    ),
                ]
            ),
        )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
