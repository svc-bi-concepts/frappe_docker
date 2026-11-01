#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash
# Keep the blessed program in this file: av executes the launcher from stdin.
# fmt: off
exec /usr/bin/python3 -I - "$@" <<'__BEXIO_SALES_INVOICE_PY__'
"""Create one reviewed sales invoice as a bexio draft. Never issue or edit it."""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

BEXIO_DIR = (
    os.environ.get("BEXIO_DIR")
    or "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/bexio"
)
sys.path.insert(0, BEXIO_DIR)
import client  # noqa: E402

PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
RESULT = "bexio-upload/last-result.txt"
PAGE_SIZE = 500
REQUESTS = {("POST", "/2.0/kb_invoice")} | {
    ("GET", path)
    for path in (
        "/3.0/users/me",
        "/2.0/language",
        "/2.0/payment_type",
        "/2.0/unit",
        "/2.0/accounts",
        "/3.0/taxes",
        "/3.0/banking/accounts",
        "/3.0/currencies",
        "/2.0/kb_invoice",
    )
}
CONTACT_PATH = re.compile(r"/2\.0/contact/[0-9]+\Z")
INVOICE_PATH = re.compile(r"/2\.0/kb_invoice/[0-9]+\Z")
INPUT_KEYS = {
    "contact_id",
    "title",
    "date",
    "due_date",
    "vat_mode",
    "bank_account_id",
    "payment_type_id",
    "language_id",
    "currency_id",
    "positions",
    "contact_sub_id",
}
POSITION_KEYS = {"text", "amount", "unit_price", "account", "tax", "unit"}
ALLOWED = {
    "title",
    "contact_id",
    "contact_sub_id",
    "user_id",
    "language_id",
    "bank_account_id",
    "currency_id",
    "payment_type_id",
    "mwst_type",
    "mwst_is_net",
    "show_position_taxes",
    "is_valid_from",
    "is_valid_to",
    "positions",
}
POSITION_ALLOWED = {
    "type",
    "amount",
    "unit_price",
    "account_id",
    "tax_id",
    "text",
    "unit_id",
}


class RefusedRequest(Exception):
    """Request is outside the reviewed API surface."""


def _send(method, path, token, body=None, query=None):
    if (method, path) not in REQUESTS and not (
        method == "GET"
        and (CONTACT_PATH.fullmatch(path) or INVOICE_PATH.fullmatch(path))
    ):
        raise RefusedRequest(f"refused {method} {path}: this script does not send it")
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
        raise SystemExit(
            "bexio refused {} {} (HTTP {}): {}".format(
                method, path, err.code, client._error_detail(err) or "no detail"
            )
        )
    except urllib.error.URLError:
        raise SystemExit(
            f"could not reach bexio on {method} {path}; no further request was sent"
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


def check_private_path(path):
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not real.startswith(
        os.path.realpath(PRIVATE) + os.sep
    ):
        raise SystemExit(
            f"refused {path}: invoice file must be under the private folder"
        )
    return real


def _date(value, key):
    try:
        parsed = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise SystemExit(f"refused invoice file: {key} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise SystemExit(f"refused invoice file: {key} must be YYYY-MM-DD")
    return parsed


def _positive(value, key):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]{1,6})?", value
    ):
        raise SystemExit(
            f"refused {key}: use a positive decimal string with at most 6 places"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation:
        amount = Decimal(0)
    if not amount.is_finite() or amount <= 0:
        raise SystemExit(f"refused {key}: value must be positive")
    return amount


def load_invoice(path):
    try:
        with open(check_private_path(path), encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError) as err:
        raise SystemExit(f"refused invoice file: {err}")
    if not isinstance(data, dict):
        raise SystemExit("refused invoice file: expected a JSON object")
    unknown = set(data) - INPUT_KEYS
    if unknown:
        raise SystemExit(
            "refused invoice file: unknown keys {}".format(", ".join(sorted(unknown)))
        )
    required = INPUT_KEYS - {"contact_sub_id"}
    missing = required - set(data)
    if missing:
        raise SystemExit(
            "refused invoice file: missing keys {}".format(", ".join(sorted(missing)))
        )
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise SystemExit("refused invoice file: title is required")
    for key in (
        "contact_id",
        "contact_sub_id",
        "bank_account_id",
        "payment_type_id",
        "language_id",
        "currency_id",
    ):
        if key in data and not (type(data[key]) is int and data[key] > 0):
            raise SystemExit(
                f"refused invoice file: {key} must be a positive integer id"
            )
    start, end = _date(data["date"], "date"), _date(data["due_date"], "due_date")
    if end < start:
        raise SystemExit("refused invoice file: due_date must be on or after date")
    if data["vat_mode"] not in {"net", "gross", "exempt"}:
        raise SystemExit("refused invoice file: vat_mode must be net, gross or exempt")
    positions = data["positions"]
    if not isinstance(positions, list) or not 1 <= len(positions) <= 100:
        raise SystemExit("refused invoice file: positions must contain 1 to 100 rows")
    normalized = []
    for index, row in enumerate(positions, 1):
        if not isinstance(row, dict):
            raise SystemExit(f"refused position {index}: expected an object")
        unknown = set(row) - POSITION_KEYS
        if unknown:
            raise SystemExit(
                "refused position {}: unknown keys {}".format(
                    index, ", ".join(sorted(unknown))
                )
            )
        if POSITION_KEYS - {"unit"} - set(row):
            raise SystemExit(f"refused position {index}: required fields are missing")
        if not isinstance(row["text"], str) or not row["text"].strip():
            raise SystemExit(f"refused position {index}: text is required")
        if (
            not isinstance(row["account"], str)
            or not row["account"]
            or not isinstance(row["tax"], str)
            or not row["tax"]
        ):
            raise SystemExit(
                f"refused position {index}: account and tax must be non-empty strings"
            )
        _positive(row["amount"], "position amount")
        _positive(row["unit_price"], "position unit_price")
        if "unit" in row and (not isinstance(row["unit"], str) or not row["unit"]):
            raise SystemExit(
                f"refused position {index}: unit must be a non-empty string"
            )
        normalized.append(dict(row))
    data["positions"] = normalized
    return data, start, end


def _one(rows, ident, label):
    matches = [row for row in rows if str(row.get("id")) == str(ident)]
    if len(matches) != 1:
        raise SystemExit(f"refused: {label} id {ident} did not resolve exactly once")
    return matches[0]


def _account(rows, number):
    matches = [r for r in rows if str(r.get("account_no")) == number]
    if len(matches) != 1:
        raise SystemExit(f"refused: account {number} did not resolve exactly once")
    account = matches[0]
    if (
        not account.get("is_active")
        or account.get("is_locked")
        or account.get("account_type") != 1
    ):
        raise SystemExit(f"refused: account {number} must be active, unlocked revenue")
    return account


def _tax(rows, code):
    matches = [r for r in rows if r.get("code") == code]
    if len(matches) != 1:
        raise SystemExit(f"refused: tax {code} did not resolve exactly once")
    return matches[0]


def _name(row):
    return (
        row.get("name") or row.get("name_1") or row.get("label") or "(name unavailable)"
    )


def _resolve(data, start, end, token):
    contact = _send("GET", "/2.0/contact/" + str(data["contact_id"]), token)
    if not isinstance(contact, dict):
        raise SystemExit("bexio contact lookup returned no contact")
    sub = None
    if "contact_sub_id" in data:
        sub = _send("GET", "/2.0/contact/" + str(data["contact_sub_id"]), token)
        if not isinstance(sub, dict):
            raise SystemExit("bexio sub contact lookup returned no contact")
    me = _send("GET", "/3.0/users/me", token)
    user_id = me.get("id") if isinstance(me, dict) else None
    if user_id is None:
        raise SystemExit("bexio did not return the current user id")
    lang = _one(_paged("/2.0/language", token), data["language_id"], "language")
    payment = _one(
        _paged("/2.0/payment_type", token), data["payment_type_id"], "payment type"
    )
    bank = _one(
        _paged("/3.0/banking/accounts", token), data["bank_account_id"], "bank account"
    )
    currency = _one(_paged("/3.0/currencies", token), data["currency_id"], "currency")
    accounts = _paged("/2.0/accounts", token)
    taxes = _paged(
        "/3.0/taxes",
        token,
        {"scope": "active", "types": "sales_tax", "date": start.isoformat()},
    )
    units = _paged("/2.0/unit", token)
    mapped = []
    for row in data["positions"]:
        account = _account(accounts, row["account"])
        tax = _tax(taxes, row["tax"])
        pos = {
            "type": "KbPositionCustom",
            "amount": row["amount"],
            "unit_price": row["unit_price"],
            "account_id": account["id"],
            "tax_id": tax["id"],
            "text": row["text"],
        }
        if "unit" in row:
            found = [u for u in units if u.get("name") == row["unit"]]
            if len(found) != 1:
                raise SystemExit(
                    "refused: unit {!r} did not resolve exactly once".format(
                        row["unit"]
                    )
                )
            pos["unit_id"] = found[0]["id"]
        mapped.append(pos)
    existing = _paged("/2.0/kb_invoice", token)
    if any(
        str(r.get("contact_id")) == str(data["contact_id"])
        and r.get("title") == data["title"]
        and str(r.get("is_valid_from"))[:10] == start.isoformat()
        for r in existing
    ):
        raise SystemExit(
            "refused: matching invoice contact, title and date already exists"
        )
    mwst_type, mwst_is_net = (
        (2, True) if data["vat_mode"] == "exempt" else (0, data["vat_mode"] == "net")
    )
    body = {
        "title": data["title"],
        "contact_id": data["contact_id"],
        "user_id": user_id,
        "language_id": data["language_id"],
        "bank_account_id": data["bank_account_id"],
        "currency_id": data["currency_id"],
        "payment_type_id": data["payment_type_id"],
        "mwst_type": mwst_type,
        "mwst_is_net": mwst_is_net,
        "show_position_taxes": False,
        "is_valid_from": start.isoformat(),
        "is_valid_to": end.isoformat(),
        "positions": mapped,
    }
    if sub is not None:
        body["contact_sub_id"] = data["contact_sub_id"]
    if set(body) - ALLOWED or any(set(p) - POSITION_ALLOWED for p in mapped):
        raise SystemExit("refused: generated body contains an unapproved field")
    print("contact {}: {}".format(data["contact_id"], _name(contact)))
    if sub is not None:
        print("sub contact {}: {}".format(data["contact_sub_id"], _name(sub)))
    for label, row in (
        ("language", lang),
        ("payment type", payment),
        ("bank account", bank),
        ("currency", currency),
    ):
        print(f"{label}: {_name(row)}")
    print(f"resolved account and tax ids for {len(mapped)} positions")
    return body


def _record(line):
    folder = os.path.join(PRIVATE, os.path.dirname(RESULT))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(PRIVATE, RESULT), "a", encoding="utf-8") as out:
        out.write(line.rstrip("\n") + "\n")


def _verify(expected, actual):
    if not isinstance(actual, dict) or actual.get("kb_item_status_id") != 7:
        raise ValueError("draft status is not 7")
    for key in (
        "contact_id",
        "contact_sub_id",
        "title",
        "is_valid_from",
        "is_valid_to",
        "mwst_type",
        "mwst_is_net",
        "bank_account_id",
        "payment_type_id",
        "language_id",
        "currency_id",
    ):
        if expected.get(key) != actual.get(key):
            raise ValueError(f"read-back {key} changed")
    expected_rows, actual_rows = expected["positions"], actual.get("positions")
    if not isinstance(actual_rows, list) or len(expected_rows) != len(actual_rows):
        raise ValueError("read-back position count changed")
    for wanted, got in zip(expected_rows, actual_rows):
        for key in ("text", "account_id", "tax_id", "amount", "unit_price"):
            if key in {"amount", "unit_price"}:
                if Decimal(str(wanted[key])) != Decimal(str(got.get(key))):
                    raise ValueError(f"read-back position {key} changed")
            elif wanted.get(key) != got.get(key):
                raise ValueError(f"read-back position {key} changed")
        total = (Decimal(wanted["amount"]) * Decimal(wanted["unit_price"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if Decimal(str(got.get("position_total"))) != total:
            raise ValueError("read-back position total changed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create one bexio sales invoice draft")
    parser.add_argument("--invoice", required=True, help="private JSON invoice file")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="create the draft after resolving the dry-run plan",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is required")
    data, start, end = load_invoice(args.invoice)
    body = _resolve(data, start, end, token)
    print(json.dumps(body, indent=2, sort_keys=True))
    if not args.yes:
        print("dry run: nothing created; add --yes to create")
        return
    created = _send("POST", "/2.0/kb_invoice", token, body=body)
    ident = created.get("id") if isinstance(created, dict) else None
    if ident is None:
        raise SystemExit(
            "bexio answered without an id; check bexio before running again"
        )
    doc = created.get("document_nr")
    record = "sales invoice id {} document_nr {}".format(ident, doc or "(none)")
    try:
        actual = _send("GET", "/2.0/kb_invoice/" + str(ident), token)
        _verify(body, actual)
    except (ValueError, RefusedRequest) as err:
        _record(record + " read-back failed")
        raise SystemExit(f"draft exists; read-back failed: {err}; do not run again")
    optional = {
        key: "yes" if actual.get(key) else "no"
        for key in ("document_nr", "header", "footer", "template_slug")
    }
    _record(
        record
        + " document_nr={} header={} footer={} template_slug={}".format(
            *(optional[k] for k in ("document_nr", "header", "footer", "template_slug"))
        )
    )
    print(f"created draft id {ident}")
    print(
        "total: {}; total_taxes: {}".format(
            actual.get("total"), actual.get("total_taxes")
        )
    )
    print(
        "document_nr/header/footer/template_slug: {}".format(
            "/".join(
                optional[k]
                for k in ("document_nr", "header", "footer", "template_slug")
            )
        )
    )


if __name__ == "__main__":
    main()
__BEXIO_SALES_INVOICE_PY__
# fmt: on
