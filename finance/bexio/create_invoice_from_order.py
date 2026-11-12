#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash
# Keep the blessed program in this file: av executes the launcher from stdin.
exec /usr/bin/python3 -I - "$@" <<'__BEXIO_ORDER_INVOICE_PY__'
"""Create one sales invoice DRAFT from an existing bexio order. Never issue or send it.

bexio's own order action (POST /2.0/kb_order/{id}/invoice) makes the invoice, so it
stays linked to the order and bexio counts the invoiced quantity off the order's open
amount. The only other write is one edit of the draft this run created, to set its
date. Accepted fields follow the published API docs and are not proven live.
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

BEXIO_DIR = (
    os.environ.get("BEXIO_DIR")
    or "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/bexio"
)
sys.path.insert(0, BEXIO_DIR)
import client  # noqa: E402

PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
RESULT = "bexio-upload/last-result.txt"
PAGE_SIZE = 500
DRAFT = 7
GETS = {"/2.0/kb_invoice", "/2.0/accounts", "/3.0/taxes"}
GET_ONE = re.compile(r"/2\.0/(kb_order|contact|kb_invoice)/[0-9]+\Z")
ORDER_INVOICE = re.compile(r"/2\.0/kb_order/([0-9]+)/invoice\Z")
INVOICE_EDIT = re.compile(r"/2\.0/kb_invoice/([0-9]+)\Z")
BILLABLE = {"KbPositionCustom", "KbPositionArticle"}
# Positions without a quantity of their own go along with the invoiced one.
CARRIED = {"KbPositionText", "KbPositionSubtotal", "KbPositionPagebreak"}
# The writable invoice fields an edit sends back unchanged, so that an edit which
# replaces the whole document cannot blank them; only the two dates change.
KEPT = (
    "title",
    "contact_id",
    "contact_sub_id",
    "user_id",
    "language_id",
    "bank_account_id",
    "currency_id",
    "payment_type_id",
    "header",
    "footer",
    "mwst_type",
    "mwst_is_net",
    "show_position_taxes",
    "template_slug",
    "reference",
)
SAME_POSITION = ("type", "text", "account_id", "tax_id", "unit_id", "unit_price")

# Writes this run may still send: one order action, then edits of its own draft only.
_sent_order_invoice = []
_created = set()


class RefusedRequest(Exception):
    """Request is outside the reviewed API surface."""


def _allowed(method, path):
    if method == "GET":
        return path in GETS or bool(GET_ONE.fullmatch(path))
    if method != "POST":
        return False
    if ORDER_INVOICE.fullmatch(path):
        return not _sent_order_invoice
    edit = INVOICE_EDIT.fullmatch(path)
    return bool(edit) and int(edit.group(1)) in _created


def _send(method, path, token, body=None, query=None):
    if not _allowed(method, path):
        raise RefusedRequest(f"refused {method} {path}: this script does not send it")
    if method == "POST" and ORDER_INVOICE.fullmatch(path):
        _sent_order_invoice.append(path)
    url = client.BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as err:
        permission = (
            "; token lacks kb_invoice_edit; no fallback was tried"
            if method == "POST" and err.code == 403
            else ""
        )
        raise SystemExit(
            "bexio refused {} {} (HTTP {}): {}".format(
                method,
                path,
                err.code,
                (client._error_detail(err) or "no detail") + permission,
            )
        )
    except (urllib.error.URLError, OSError, ValueError):
        advice = (
            "; the write may have happened; check bexio before running again"
            if method == "POST"
            else ""
        )
        raise SystemExit(
            f"could not read bexio response on {method} {path}; "
            f"no further request was sent{advice}"
        )


def _paged(path, token, query=None):
    rows, offset = [], 0
    while True:
        params = dict(query or {})
        params.update(limit=PAGE_SIZE, offset=offset)
        page = _send("GET", path, token, query=params)
        if not isinstance(page, list):
            raise SystemExit(f"bexio answered {path} with something other than a list")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += len(page)


def _id(value):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value or ""):
        raise argparse.ArgumentTypeError("must be a positive integer id")
    return int(value)


def _quantity(value):
    # The order action takes an integer amount per position (published docs).
    if not re.fullmatch(r"[1-9][0-9]{0,5}", value or ""):
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return int(value)


def _date(value):
    try:
        parsed = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD")
    return parsed


def _dec(value, label):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        raise SystemExit(f"refused: bexio gave no number for {label}")
    return number


def _name(row):
    return (
        row.get("name") or row.get("name_1") or row.get("label") or "(name unavailable)"
    )


def _first_line(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return " ".join(text.split())[:80] or "(no text)"


def _position(order, chosen):
    """The one billable position this invoice bills, and the positions sent with it."""
    positions = order.get("positions")
    if not isinstance(positions, list) or not positions:
        raise SystemExit("refused: the order has no positions")
    other = sorted(
        {p.get("type") for p in positions} - BILLABLE - CARRIED, key=str
    )
    if other:
        raise SystemExit(
            "refused: the order has positions this tool does not handle: "
            + ", ".join(map(str, other))
        )
    billable = [p for p in positions if p.get("type") in BILLABLE]
    if chosen is None:
        if len(billable) != 1:
            raise SystemExit(
                "refused: the order has {} billable positions; name one with "
                "--position <id> ({})".format(
                    len(billable), ", ".join(str(p.get("id")) for p in billable)
                )
            )
        return billable[0], positions
    matches = [p for p in billable if p.get("id") == chosen]
    if len(matches) != 1:
        raise SystemExit(f"refused: position {chosen} is no billable position of the order")
    return matches[0], positions


def _print_order(order, contact, positions, taxes, accounts):
    print(
        "order {} ({}): {}".format(
            order.get("id"), order.get("document_nr") or "no number", order.get("title")
        )
    )
    print("contact {}: {}".format(order.get("contact_id"), _name(contact)))
    mode = {0: "net" if order.get("mwst_is_net") else "gross", 2: "exempt"}.get(
        order.get("mwst_type"), "mwst_type {}".format(order.get("mwst_type"))
    )
    print(
        "order totals ({}): net {}, tax {}, total {}".format(
            mode, order.get("total_net"), order.get("total_taxes"), order.get("total")
        )
    )
    for p in positions:
        line = "  position {} {}: {}".format(
            p.get("id"), p.get("type"), _first_line(p.get("text"))
        )
        if p.get("type") in BILLABLE:
            tax = taxes.get(p.get("tax_id"), {})
            line += "; amount {} {} (open {}) x {}; account {}; tax {}".format(
                p.get("amount"),
                p.get("unit_name") or "",
                p.get("amount_open", "unknown"),
                p.get("unit_price"),
                accounts.get(p.get("account_id"), p.get("account_id")),
                tax.get("code", p.get("tax_id")),
            )
        print(line)
    return mode


def _plan(order, position, positions, quantity, mode, taxes):
    """The order action's body, and the printed net and tax of the invoiced position."""
    ordered = _dec(position.get("amount"), "the position amount")
    open_amount = position.get("amount_open")
    limit = ordered if open_amount is None else _dec(open_amount, "the open amount")
    if Decimal(quantity) > limit:
        raise SystemExit(
            f"refused: quantity {quantity} is more than the position's open {limit}"
        )
    price = _dec(position.get("unit_price"), "the unit price")
    tax = taxes.get(position.get("tax_id"))
    if tax is None:
        raise SystemExit("refused: the position's tax is no active sales tax at --date")
    rate = _dec(tax.get("value", 0), "the tax rate")
    line = Decimal(quantity) * price
    estimate = None
    if mode == "exempt":
        estimate = line, Decimal(0)
    elif mode == "gross":
        vat = line * rate / (100 + rate)
        estimate = line - vat, vat
    elif mode == "net":
        estimate = line, line * rate / 100
    cent = Decimal("0.01")
    print(
        "invoice: position {}, quantity {} of {} at {}; tax {} {}%".format(
            position.get("id"), quantity, ordered, price, tax.get("code"), rate
        )
    )
    if estimate is None:
        # Only the modes create_sales_invoice writes are known; a guessed figure for
        # another mwst_type would read as a checked one.
        print(f"estimate: none for {mode}; bexio computes the totals")
    else:
        print(
            "estimate (bexio computes the totals): net {}, tax {}".format(
                estimate[0].quantize(cent), estimate[1].quantize(cent)
            )
        )
    sent = []
    for p in positions:
        if p is position:
            sent.append({"id": p["id"], "type": p["type"], "amount": quantity})
        elif p.get("type") in CARRIED:
            sent.append({"id": p["id"], "type": p["type"]})
    return {"positions": sent}


def record_result(line):
    folder = os.path.join(PRIVATE, os.path.dirname(RESULT))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(PRIVATE, RESULT), "a", encoding="utf-8") as out:
        out.write(line.rstrip("\n") + "\n")


def _verify(invoice, order, position, quantity, carried):
    """The draft holds the order's contact and position, at the asked quantity."""
    if not isinstance(invoice, dict) or invoice.get("kb_item_status_id") != DRAFT:
        raise ValueError("draft status is not 7")
    if invoice.get("contact_id") != order.get("contact_id"):
        raise ValueError("read-back contact_id changed")
    rows = invoice.get("positions")
    if not isinstance(rows, list):
        raise ValueError("read-back has no positions")
    billable = [r for r in rows if isinstance(r, dict) and r.get("type") in BILLABLE]
    if len(billable) != 1:
        raise ValueError("read-back holds {} billable positions".format(len(billable)))
    got = billable[0]
    for key in SAME_POSITION:
        if key == "unit_price":
            if _dec(got.get(key), key) != _dec(position.get(key), key):
                raise ValueError("read-back position unit_price changed")
        elif got.get(key) != position.get(key):
            raise ValueError(f"read-back position {key} changed")
    if _dec(got.get("amount"), "amount") != Decimal(quantity):
        raise ValueError("read-back position amount is not the quantity")
    if sum(1 for r in rows if isinstance(r, dict) and r.get("type") in CARRIED) != carried:
        raise ValueError("read-back text positions changed")


def _edit_body(invoice, date):
    """The draft's own fields, with its dates moved to --date and the term kept."""
    start = datetime.date.fromisoformat(str(invoice.get("is_valid_from"))[:10])
    end = datetime.date.fromisoformat(str(invoice.get("is_valid_to"))[:10])
    body = {key: invoice[key] for key in KEPT if key in invoice}
    if invoice.get("project_id") is not None:
        body["pr_project_id"] = invoice["project_id"]
    body["is_valid_from"] = date.isoformat()
    body["is_valid_to"] = (date + (end - start)).isoformat()
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create one bexio sales invoice draft from an existing order"
    )
    parser.add_argument("--order", required=True, type=_id, help="bexio order id")
    parser.add_argument(
        "--contact", required=True, type=_id, help="the contact id the order must have"
    )
    parser.add_argument(
        "--date", required=True, type=_date, help="invoice date, YYYY-MM-DD"
    )
    parser.add_argument(
        "--quantity",
        type=_quantity,
        help="how many of the position's units to invoice; no default",
    )
    parser.add_argument(
        "--position",
        type=_id,
        help="the order position to bill, when the order has more than one",
    )
    parser.add_argument(
        "--yes", action="store_true", help="create the draft after the dry-run plan"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is required")

    order = _send("GET", "/2.0/kb_order/" + str(args.order), token)
    if not isinstance(order, dict) or order.get("id") != args.order:
        raise SystemExit("bexio order lookup returned no order")
    if order.get("contact_id") != args.contact:
        raise SystemExit(
            "refused: order {} belongs to contact {}, not {}".format(
                args.order, order.get("contact_id"), args.contact
            )
        )
    position, positions = _position(order, args.position)
    contact = _send("GET", "/2.0/contact/" + str(args.contact), token)
    if not isinstance(contact, dict):
        raise SystemExit("bexio contact lookup returned no contact")
    taxes = {
        t.get("id"): t
        for t in _paged(
            "/3.0/taxes",
            token,
            {"scope": "active", "types": "sales_tax", "date": args.date.isoformat()},
        )
    }
    accounts = {a.get("id"): a.get("account_no") for a in _paged("/2.0/accounts", token)}
    mode = _print_order(order, contact, positions, taxes, accounts)
    if args.quantity is None:
        raise SystemExit(
            "refused: --quantity is required: how many of position {}'s {} to invoice; "
            "this tool does not guess".format(position.get("id"), position.get("amount"))
        )
    body = _plan(order, position, positions, args.quantity, mode, taxes)
    if any(
        r.get("contact_id") == args.contact
        and r.get("title") == order.get("title")
        and str(r.get("is_valid_from"))[:10] == args.date.isoformat()
        for r in _paged("/2.0/kb_invoice", token)
    ):
        raise SystemExit("refused: an invoice with this contact, title and date exists")
    print(json.dumps(body, indent=2, sort_keys=True))
    if not args.yes:
        print("dry run: nothing created; add --yes to create")
        return

    created = _send(
        "POST", "/2.0/kb_order/{}/invoice".format(args.order), token, body=body
    )
    ident = created.get("id") if isinstance(created, dict) else None
    if type(ident) is not int:
        raise SystemExit("bexio answered without an id; check bexio before running again")
    _created.add(ident)
    record = "sales invoice id {} from order id {}".format(ident, args.order)
    try:
        record_result(record + " read-back pending")
    except OSError:
        raise SystemExit(
            f"draft id {ident} exists; could not record result; do not run again"
        )
    carried = len(body["positions"]) - 1
    try:
        actual = _send("GET", "/2.0/kb_invoice/" + str(ident), token)
        _verify(actual, order, position, args.quantity, carried)
        if str(actual.get("is_valid_from"))[:10] != args.date.isoformat():
            edit = _edit_body(actual, args.date)
            _send("POST", "/2.0/kb_invoice/" + str(ident), token, body=edit)
            actual = _send("GET", "/2.0/kb_invoice/" + str(ident), token)
            _verify(actual, order, position, args.quantity, carried)
            for key, value in edit.items():
                if key != "pr_project_id" and actual.get(key) != value:
                    raise ValueError(f"read-back {key} changed by the date edit")
    except (
        ValueError,
        TypeError,
        InvalidOperation,
        RefusedRequest,
        SystemExit,
        OSError,
    ) as err:
        try:
            record_result(record + " read-back failed")
        finally:
            raise SystemExit(
                f"draft id {ident} exists; read-back failed: {err}; do not run again"
            )
    doc = actual.get("document_nr") or "(none)"
    try:
        record_result(f"{record} document_nr {doc} draft verified")
    except OSError:
        raise SystemExit(
            f"draft id {ident} exists; could not record read-back result; do not run again"
        )
    print(f"created draft id {ident}, document_nr {doc}")
    print(
        "from order id {} ({}), position {} quantity {}".format(
            args.order, order.get("document_nr"), position.get("id"), args.quantity
        )
    )
    print(
        "date {} due {}".format(
            actual.get("is_valid_from"), actual.get("is_valid_to")
        )
    )
    print(
        "total_net: {}; total_taxes: {}; total: {}".format(
            actual.get("total_net"), actual.get("total_taxes"), actual.get("total")
        )
    )


if __name__ == "__main__":
    main()
__BEXIO_ORDER_INVOICE_PY__
