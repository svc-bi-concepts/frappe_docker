#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash
# Keep the blessed program in this file: av executes the launcher from stdin.
exec /usr/bin/python3 -I - "$@" <<'__BEXIO_PREPARE_PAYMENT_PY__'
"""Prepare one full, domestic supplier bill payment in bexio for review.

Dry run is the default. A real run books a draft if needed, creates one open
payment and reads its status back. It never sends or releases a payment.
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

BEXIO_DIR = os.environ.get("BEXIO_DIR") or "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/bexio"
sys.path.insert(0, BEXIO_DIR)
import client  # noqa: E402

REQUESTS = {
    ("GET", "/4.0/purchase/bills"),
    ("GET", "/4.0/purchase/outgoing-payments"),
    ("GET", "/3.0/banking/accounts"),
    ("POST", "/4.0/purchase/outgoing-payments"),
}
BILL_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
BILL_PATH = re.compile(r"/4\.0/purchase/bills/" + BILL_ID.pattern)
PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
PAYMENT_KEYS = {"type", "iban", "name", "street", "house_no", "postcode", "city", "country", "message", "reference_no", "execution_date"}
ADDRESS_KEYS = ("name", "street", "house_no", "postcode", "city", "country")
RESULT_FOLDER = "bexio-upload"
RESULT_FILE = "last-result.txt"
CHF_CURRENCY_ID = 1  # Bexio's seeded CHF currency id, used by the finance import/export.


class RefusedRequest(Exception):
    """An API request outside the reviewed set; never sent."""


def _send(method, path, token, body=None, query=None):
    if (method, path) not in REQUESTS and not (method == "GET" and BILL_PATH.fullmatch(path)) and not (method == "PUT" and re.fullmatch(r"/4\.0/purchase/bills/" + BILL_ID.pattern[:-2] + r"/bookings/BOOKED", path)):
        raise RefusedRequest("refused {} {}: this script does not send it".format(method, path))
    url = client.BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "yardr-finance-bexio-payment/1.0")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as err:
        if err.code == 403:
            raise SystemExit("bexio refused {} {} (HTTP 403): a token right is missing; ask Benchi to grant it".format(method, path))
        raise SystemExit("bexio refused {} {} (HTTP {}): {}".format(method, path, err.code, client._error_detail(err) or "no detail"))
    except urllib.error.URLError:
        raise SystemExit("could not reach bexio on {} {}; no further request was sent".format(method, path))


def record_result(line):
    folder = os.path.join(PRIVATE, RESULT_FOLDER)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, RESULT_FILE), "a") as f:
        f.write(line.rstrip("\n") + "\n")


def check_private_path(path):
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not real.startswith(os.path.realpath(PRIVATE) + os.sep):
        raise SystemExit("refused {}: payment file must be under the private folder".format(path))
    return real


def load_payment(path):
    try:
        with open(check_private_path(path), encoding="utf-8") as f:
            payment = json.load(f)
    except (OSError, ValueError) as err:
        raise SystemExit("refused payment file: {}".format(err))
    if not isinstance(payment, dict):
        raise SystemExit("refused payment file: expected a JSON object")
    unknown = set(payment) - PAYMENT_KEYS
    if unknown:
        raise SystemExit("refused payment file: unknown keys {}".format(", ".join(sorted(unknown))))
    if payment.get("type") not in {"IBAN", "QR"}:
        raise SystemExit("refused payment file: type must be IBAN or QR")
    for key, value in payment.items():
        if not isinstance(value, str):
            raise SystemExit("refused payment file: {} must be a string".format(key))
    for key in ADDRESS_KEYS:
        if not payment.get(key, "").strip():
            raise SystemExit("refused payment file: {} is required".format(key))
    iban = re.sub(r"\s+", "", payment.get("iban", "")).upper()
    if not iban or not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        raise SystemExit("refused payment file: receiver IBAN is invalid")
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if int(numeric) % 97 != 1:
        raise SystemExit("refused payment file: receiver IBAN check digits are invalid")
    if payment["type"] == "IBAN":
        if iban[:2] != "CH" or len(iban) != 21 or not iban[4:9].isdigit():
            raise SystemExit("refused payment file: foreign IBAN payments are not supported")
        if 30000 <= int(iban[4:9]) <= 31999:
            raise SystemExit("refused payment file: QR-IBAN requires type QR")
        if payment.get("reference_no"):
            raise SystemExit("refused payment file: reference_no is only valid for QR")
    else:
        if iban[:2] != "CH" or len(iban) != 21 or not iban[4:9].isdigit():
            raise SystemExit("refused payment file: QR payment requires a Swiss QR-IBAN")
        if not payment.get("reference_no", "").strip():
            raise SystemExit("refused payment file: QR requires reference_no")
        if payment.get("message"):
            raise SystemExit("refused payment file: message is only valid for IBAN")
        if not 30000 <= int(iban[4:9]) <= 31999:
            raise SystemExit("refused payment file: QR requires a QR-IBAN with a creditor reference")
    payment = dict(payment)
    payment["iban"] = iban
    return payment


def _list_bills(token, supplier_id=None):
    rows, page = [], 1
    while True:
        query = {"page": page, "limit": 500}
        if supplier_id is not None:
            query["supplier_id"] = supplier_id
        result = _send("GET", "/4.0/purchase/bills", token, query=query)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise SystemExit("bexio answered the bill list without rows")
        rows.extend(data)
        if len(data) < 500:
            return rows
        page += 1


def _payments(token, bill_id):
    rows, page = [], 1
    while True:
        result = _send("GET", "/4.0/purchase/outgoing-payments", token,
                       query={"bill_id": bill_id, "page": page, "limit": 500})
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise SystemExit("bexio answered outgoing payments without rows")
        rows.extend(data)
        if len(data) < 500:
            return rows
        page += 1


def known_ibans(token, supplier_id):
    found = set()
    for bill in _list_bills(token, supplier_id):
        bill_id = bill.get("id")
        if isinstance(bill_id, str) and BILL_ID.fullmatch(bill_id):
            found.update(str(p.get("receiver_iban", "")).replace(" ", "").upper()
                         for p in _payments(token, bill_id) if p.get("receiver_iban"))
    return found


def checked_execution_date(value, bill, today=None):
    today = today or datetime.date.today()
    source = value or bill.get("due_date")
    if not source:
        raise SystemExit("refused: execution date and bill due_date are both missing")
    try:
        day = datetime.date.fromisoformat(source)
        bill_day = datetime.date.fromisoformat(bill["bill_date"])
    except (ValueError, TypeError, KeyError):
        raise SystemExit("refused: bill dates and execution_date must use YYYY-MM-DD")
    day = max(day, today, bill_day)
    if day.weekday() == 5:
        day -= datetime.timedelta(days=1)
    elif day.weekday() == 6:
        day -= datetime.timedelta(days=2)
    if day < max(today, bill_day):
        # A weekend at the earliest allowed date rolls forward to Monday.
        earliest = max(today, bill_day)
        day = earliest + datetime.timedelta(days=7 - earliest.weekday())
    return day


def bank_accounts(token):
    rows, offset = [], 0
    while True:
        data = _send("GET", "/3.0/banking/accounts", token, query={"limit": 500, "offset": offset})
        if not isinstance(data, list):
            raise SystemExit("bexio answered banking accounts without a list")
        rows.extend(data)
        if len(data) < 500:
            return rows
        offset += len(data)


def payment_body(bill, account, payment, execution_date):
    try:
        amount = Decimal(str(bill.get("pending_amount")))
    except InvalidOperation:
        amount = Decimal("0")
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise SystemExit("refused: bill pending_amount is missing or invalid")
    body = {"bill_id": bill["id"], "payment_type": payment["type"],
            "execution_date": execution_date.isoformat(), "amount": float(amount),
            "currency_code": "CHF", "exchange_rate": 1,
            "sender_bank_account_id": account["id"], "is_salary_payment": False}
    sender_fields = {"iban_nr": "sender_iban", "owner": "sender_name", "owner_address": "sender_street",
                     "owner_house_number": "sender_house_no", "owner_zip": "sender_postcode", "owner_city": "sender_city",
                     "owner_country_code": "sender_country_code"}
    address = str(account.get("owner_address") or "")
    house = str(account.get("owner_house_number") or "")
    if house and address.endswith(" " + house):
        address = address[: -(len(house) + 1)]
    account = dict(account, owner_address=address)
    for source, target in sender_fields.items():
        if account.get(source):
            body[target] = str(account[source])
    receiver_fields = {"iban": "receiver_iban", "name": "receiver_name", "street": "receiver_street",
                       "house_no": "receiver_house_no", "postcode": "receiver_postcode", "city": "receiver_city",
                       "country": "receiver_country_code"}
    for source, target in receiver_fields.items():
        body[target] = payment[source]
    if payment["type"] == "IBAN":
        body["fee_type"] = "NO_FEE"
        if payment.get("message"):
            body["message"] = payment["message"]
    else:
        body["reference_no"] = payment["reference_no"]
    return body


def validate_bill(bill, outgoing_payments):
    if bill.get("status") not in {"DRAFT", "BOOKED"}:
        raise SystemExit("refused: bill status must be DRAFT or BOOKED")
    if bill.get("status") == "DRAFT" and bill.get("amount_calc") is None:
        raise SystemExit("refused: DRAFT amount_calc is null; repair with create_contact_and_bill.py --repair before booking")
    if bill.get("currency_code") != "CHF":
        raise SystemExit("refused: only CHF bills are supported")
    if any(p.get("status") != "ERROR" for p in outgoing_payments):
        raise SystemExit("refused: bill already has an open outgoing payment")


def validate_bank_account(matches):
    if len(matches) != 1 or matches[0].get("type") != "bank" or matches[0].get("currency_id") != CHF_CURRENCY_ID:
        raise SystemExit("refused: sender account must be a CHF bank account")
    return matches[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare one open supplier bill payment in bexio.")
    parser.add_argument("--bill", required=True, help="bill document number")
    parser.add_argument("--payment", required=True, help="private payment JSON file")
    parser.add_argument("--bank-account-id", required=True, type=int)
    parser.add_argument("--accept-new-iban", action="store_true", help="yardmaster only, after Benchi confirms this bill")
    parser.add_argument("--yes", action="store_true", help="book if needed and create the open payment")
    args = parser.parse_args(argv)
    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is not set; run through av inject")
    payment = load_payment(args.payment)
    bills = [b for b in _list_bills(token) if str(b.get("document_no", "")) == args.bill]
    if len(bills) != 1:
        raise SystemExit("refused: expected one bill with that document number; found {}".format(len(bills)))
    brief = bills[0]
    bill_id = brief.get("id")
    if not isinstance(bill_id, str) or not BILL_ID.fullmatch(bill_id):
        raise SystemExit("refused: bill id is not a UUID")
    bill = _send("GET", "/4.0/purchase/bills/" + bill_id, token)
    if not isinstance(bill, dict):
        raise SystemExit("bexio answered the bill read without an object")
    existing = _payments(token, bill_id)
    validate_bill(bill, existing)
    accounts = [a for a in bank_accounts(token) if str(a.get("id")) == str(args.bank_account_id)]
    sender_account = validate_bank_account(accounts)
    history = known_ibans(token, bill.get("supplier_id"))
    if payment["iban"] in history:
        print("receiver IBAN: matches supplier payment history")
    elif history:
        if not args.accept_new_iban:
            raise SystemExit("refused: receiver IBAN differs from supplier payment history; yardmaster must confirm with Benchi and pass --accept-new-iban")
        print("receiver IBAN: new for supplier; accepted by yardmaster for this bill")
    else:
        print("first payment to this IBAN")
    execution_date = checked_execution_date(payment.get("execution_date"), bill)
    body = payment_body(bill, sender_account, payment, execution_date)
    print("bill {}: {}".format(args.bill, bill["status"]))
    print("step: {}".format("book as BOOKED, then create one open payment" if bill["status"] == "DRAFT" else "create one open payment"))
    print("checks: CHF bill; domestic receiver; sender is CHF bank account; full pending amount; no open payment")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if not args.yes:
        print("dry run: nothing sent; add --yes to book and create the open payment")
        return
    if bill["status"] == "DRAFT":
        _send("PUT", "/4.0/purchase/bills/{}/bookings/BOOKED".format(bill_id), token)
    # Never retry POST: the server may have stored it before a connection failed.
    created = _send("POST", "/4.0/purchase/outgoing-payments", token, body=body)
    if not isinstance(created, dict) or not created.get("id"):
        raise SystemExit("bexio answered without a payment id; check outgoing payments before running again")
    status_rows = _payments(token, bill_id)
    created_row = next((p for p in status_rows if str(p.get("id")) == str(created["id"])), None)
    if not created_row:
        raise SystemExit("payment created but not found in read-back; check outgoing payments")
    status = str(created_row.get("status", "unknown"))
    print("outgoing payment status: {}".format(status))
    record_result("prepared bill payment: status={}, first_to_iban={}".format(status, payment["iban"] not in history))


if __name__ == "__main__":
    main(sys.argv[1:])
__BEXIO_PREPARE_PAYMENT_PY__
