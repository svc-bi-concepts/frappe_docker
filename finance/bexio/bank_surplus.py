"""Compare one MT940 statement with ERPNext Bank Transactions and write a private cancel list.

This report is read-only. The name list is accepted by import_bank.py --cancel; that command
still checks every row and only cancels with --confirm.
"""

import argparse
import collections
import datetime
import os
import sys

import import_master as im
from import_bank import ZERO, cents, write_private

# Reuse the app's parser; loading it this way keeps this host-side script independent of Frappe.
APP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "finance",
    "apps",
    "bi_finance",
)
sys.path.insert(0, APP_ROOT)
from bi_finance import mt940  # noqa: E402


def _key(row):
    deposit, withdrawal = cents(row.get("deposit") or 0), cents(
        row.get("withdrawal") or 0
    )
    return (
        row.get("date") or row["value_date"],
        deposit if deposit else withdrawal,
        "in" if deposit else "out",
    )


def _net(row):
    return cents(row.get("deposit") or 0) - cents(row.get("withdrawal") or 0)


def check_statement(statement):
    opening = statement["balances"].get("OPBD")
    closing = statement["balances"].get("CLBD")
    if not opening or not closing:
        raise ValueError("MT940 needs opening and closing balances")
    total = sum(
        (
            cents(tx["deposit"]) - cents(tx["withdrawal"])
            for tx in statement["transactions"]
        ),
        ZERO,
    )
    if cents(opening["amount"] + total) != cents(closing["amount"]):
        raise ValueError(
            "MT940 opening balance plus transactions does not equal closing balance"
        )


def pair(bank_lines, rows):
    """Return surplus candidates, conflicts, and unmatched row/statement counts."""
    bank = collections.Counter(_key(line) for line in bank_lines)
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[_key(row)].append(row)
    surplus, conflicts = [], []
    for key, group in grouped.items():
        # An absent bank key is unexplained, not proof of a surplus copy.
        if key not in bank:
            continue
        group.sort(
            key=lambda row: (
                not bool(
                    row.get("allocated_amount")
                    and cents(row["allocated_amount"]) != ZERO
                    or row.get("payment_entries")
                ),
                not bool(row.get("transaction_id")),
                row.get("status") != "Unreconciled",
                row.get("status") != "Reconciled",
                row["name"],
            )
        )
        for row in group[bank[key] :]:
            if (
                (row.get("allocated_amount") and cents(row["allocated_amount"]) != ZERO)
                or row.get("payment_entries")
                or row.get("status") != "Unreconciled"
                or row.get("transaction_id")
            ):
                conflicts.append(row)
            else:
                surplus.append(row)
    no_bank = sum(len(group) for key, group in grouped.items() if key not in bank)
    no_row = sum(
        max(0, count - len(grouped.get(key, ()))) for key, count in bank.items()
    )
    return (
        sorted(surplus, key=lambda row: (row["date"], row["name"])),
        conflicts,
        no_bank,
        no_row,
    )


def report_text(bank_lines, rows, surplus, conflicts, no_bank, no_row):
    lines = [
        f"bank lines: {len(bank_lines)}",
        f"rows: {len(rows)}",
        f"kept: {len(rows) - len(surplus) - len(conflicts) - no_bank}",
        f"surplus: {len(surplus)}",
        f"conflicts: {len(conflicts)}",
        f"rows with no bank line: {no_bank}",
        f"bank lines with no row: {no_row}",
        "",
        "year  bank net  rows before  rows after",
    ]
    years = sorted(
        {line["value_date"][:4] for line in bank_lines}
        | {row["date"][:4] for row in rows}
    )
    surplus_names = {row["name"] for row in surplus}
    for year in years:
        bnet = sum(
            (
                _net({"deposit": line["deposit"], "withdrawal": line["withdrawal"]})
                for line in bank_lines
                if line["value_date"].startswith(year)
            ),
            ZERO,
        )
        before = sum((_net(row) for row in rows if row["date"].startswith(year)), ZERO)
        after = sum(
            (
                _net(row)
                for row in rows
                if row["date"].startswith(year) and row["name"] not in surplus_names
            ),
            ZERO,
        )
        lines.append(f"{year}  {bnet:.2f}  {before:.2f}  {after:.2f}")
    return "\n".join(lines)


def private_path(path):
    root = os.path.realpath(im.PRIVATE)
    resolved = os.path.realpath(path)
    if os.path.commonpath((root, resolved)) != root:
        raise ValueError("MT940 file must be under private/")
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only comparison of MT940 lines and submitted ERPNext Bank Transactions"
    )
    parser.add_argument("--mt940", required=True)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--bank-account", required=True)
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    try:
        path = private_path(args.mt940)
        datetime.date.fromisoformat(args.date_from)
        datetime.date.fromisoformat(args.date_to)
        if args.date_from > args.date_to:
            raise ValueError("--from must be on or before --to")
        with open(path, "rb") as source:
            statement = mt940.parse(source.read())
        check_statement(statement)
        bank_lines = [
            tx
            for tx in statement["transactions"]
            if args.date_from <= tx["value_date"] <= args.date_to
        ]
        erp = im.Erp.from_file(args.token_file)
        rows = erp.list(
            "Bank Transaction",
            [
                ["bank_account", "=", args.bank_account],
                ["docstatus", "=", 1],
                ["date", ">=", args.date_from],
                ["date", "<=", args.date_to],
            ],
            [
                "name",
                "date",
                "deposit",
                "withdrawal",
                "status",
                "allocated_amount",
                "transaction_id",
            ],
        )
        for row in rows:
            row["payment_entries"] = (
                erp.get("Bank Transaction", row["name"]).get("payment_entries") or []
            )
        surplus, conflicts, no_bank, no_row = pair(bank_lines, rows)
        stamp = datetime.date.today().isoformat()
        root = os.path.realpath(im.PRIVATE)
        write_private(
            os.path.join(root, f"bexio-bank-surplus-{stamp}.txt"),
            "\n".join(row["name"] for row in surplus),
        )
        write_private(
            os.path.join(root, f"bexio-bank-surplus-{stamp}-report.txt"),
            report_text(bank_lines, rows, surplus, conflicts, no_bank, no_row),
        )
        print(
            f"bank lines: {len(bank_lines)}; rows: {len(rows)}; kept: {len(rows)-len(surplus)-len(conflicts)-no_bank}; surplus: {len(surplus)}; conflicts: {len(conflicts)}; rows without bank line: {no_bank}; bank lines without row: {no_row}"
        )
        return 0
    except (OSError, ValueError, im.ErpError) as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
