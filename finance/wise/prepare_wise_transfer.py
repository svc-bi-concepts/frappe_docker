#!/usr/local/bin/av inject +FINANCE_WISE_TRANSFER_TOKEN /bin/bash
exec /usr/bin/python3 -I - "$@" <<'__WISE_PREPARE_TRANSFER_PY__'
"""Prepare one unfunded Wise transfer. Funding remains a manual UI action."""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation

BASE_URL = "https://api.wise.com"
API_VERSION = "2026Q4"
PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
REQUESTS = {
    ("GET", "/2026Q4/profiles"),
    ("GET", "/2026Q4/accounts/{id}"),
    ("GET", "/2026Q4/transfers"),
    ("GET", "/2026Q4/transfers/{id}"),
    ("POST", "/2026Q4/profiles/{id}/quotes"),
    ("POST", "/2026Q4/transfer-requirements"),
    ("POST", "/2026Q4/transfers"),
}
DETAIL_KEYS = {"transferPurpose", "transferPurposeSubTransferPurpose", "transferPurposeInvoiceNumber", "sourceOfFunds"}
FINAL_UNPAID = {"cancelled", "funds_refunded", "charged_back"}
NAMESPACE = uuid.UUID("0f61d5f9-1659-4a0c-9ea9-f6eb2c9fc78b")


class RefusedRequest(Exception):
    """A request outside the reviewed API surface."""


def _template(method, path):
    path = re.sub(r"(?<=/profiles/)\d+(?=/quotes\Z)", "{id}", path)
    path = re.sub(r"(?<=/accounts/)\d+\Z", "{id}", path)
    path = re.sub(r"(?<=/transfers/)\d+\Z", "{id}", path)
    return method, path


def _send(method, path, token, body=None, query=None):
    if "?" in path or _template(method, path) not in REQUESTS:
        raise RefusedRequest("refused {} {}: this script does not send it".format(method, path))
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as err:
        codes = []
        approval = False
        try:
            payload = json.loads(err.read().decode("utf-8"))
            errors = payload.get("errors", [])
            codes = [str(e["code"]) for e in errors if isinstance(e, dict) and e.get("code")]
            approval = "missing approval" in json.dumps(payload).casefold()
        except (ValueError, AttributeError, TypeError):
            pass
        if approval:
            raise SystemExit("a payment approval rule on the account blocks API transfers; Benchi must change this")
        message = "Wise refused {} {} (HTTP {})".format(method, path, err.code)
        if codes:
            message += " ({})".format(", ".join(codes))
        raise SystemExit(message)
    except urllib.error.URLError:
        raise SystemExit("could not reach Wise on {} {}; no further request was sent".format(method, path))


def record_result(outcome, transfer_id="", status=""):
    folder = os.path.join(PRIVATE, "wise")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "last-result.txt"), "a", encoding="utf-8") as result:
        result.write("outcome: {}; transfer_id: {}; status: {}\n".format(outcome, transfer_id, status))


def amount_value(raw):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        raise SystemExit("refused: amount must be a positive number with at most two decimals")
    if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
        raise SystemExit("refused: amount must be a positive number with at most two decimals")
    return value


def read_transfers(profile_id, token, recipient_id):
    rows, offset = [], 0
    while True:
        page = _send("GET", "/{}/transfers".format(API_VERSION), token,
                     query={"profile": profile_id, "limit": 100, "offset": offset})
        if not isinstance(page, list):
            raise SystemExit("Wise answered the transfer list without rows")
        rows.extend(row for row in page if str(row.get("targetAccount")) == str(recipient_id))
        if len(page) < 100:
            return rows
        offset += len(page)


def reference_of(row):
    return [str(value) for value in (row.get("reference"), (row.get("details") or {}).get("reference")) if value]


def transaction_id(recipient_id, invoice, attempt):
    name = "{}:{}".format(recipient_id, invoice.strip())
    if attempt > 1:
        name += ":{}".format(attempt)
    return str(uuid.uuid5(NAMESPACE, name))


def transfer_body(args, source_currency):
    details = {"reference": args.invoice.strip()}
    details.update(dict(args.detail))
    return {"targetAccount": args.recipient_id, "quoteUuid": "<quote UUID>",
            "customerTransactionId": transaction_id(args.recipient_id, args.invoice, args.attempt),
            "details": details}


def validate_requirements(answer, details):
    groups = answer if isinstance(answer, list) else answer.get("requirements", []) if isinstance(answer, dict) else []
    for group in groups:
        fields = group.get("fields", []) if isinstance(group, dict) else []
        for field in fields:
            key = field.get("key") or field.get("name")
            if field.get("required") and key and not str(details.get(key, "")).strip():
                raise SystemExit("refused: required field {} is empty; supply it with --detail".format(key))
            if key == "reference":
                value = details.get("reference", "")
                constraints = field.get("constraints", {})
                if constraints.get("maxLength") is not None and len(value) > constraints["maxLength"]:
                    raise SystemExit("refused: reference exceeds Wise maxLength; do not shorten it")
                if constraints.get("minLength") is not None and len(value) < constraints["minLength"]:
                    raise SystemExit("refused: reference is below Wise minLength")
                regexp = constraints.get("validationRegexp")
                if regexp and not re.fullmatch(regexp, value):
                    raise SystemExit("refused: reference does not match Wise validationRegexp")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare one unfunded Wise transfer.")
    parser.add_argument("--recipient-id", required=True, type=int)
    parser.add_argument("--invoice", required=True)
    parser.add_argument("--amount", required=True)
    parser.add_argument("--target-currency", required=True)
    parser.add_argument("--source-currency")
    parser.add_argument("--planned-date", required=True)
    parser.add_argument("--detail", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    token = os.environ.get("FINANCE_WISE_TRANSFER_TOKEN")
    if not token:
        raise SystemExit("FINANCE_WISE_TRANSFER_TOKEN is not set; run through av inject")
    amount = amount_value(args.amount)
    if args.attempt < 1:
        raise SystemExit("refused: --attempt must be at least 1")
    if not args.invoice.strip():
        raise SystemExit("refused: invoice reference is required")
    try:
        planned = datetime.date.fromisoformat(args.planned_date)
        if planned.isoformat() != args.planned_date:
            raise ValueError()
    except ValueError:
        raise SystemExit("refused: planned date must use YYYY-MM-DD")
    detail = []
    for item in args.detail:
        if "=" not in item:
            raise SystemExit("refused: --detail must be KEY=VALUE")
        key, value = item.split("=", 1)
        if key not in DETAIL_KEYS:
            raise SystemExit("refused: unsupported --detail key {}".format(key))
        detail.append((key, value))
    args.detail = detail
    profiles = _send("GET", "/{}/profiles".format(API_VERSION), token)
    businesses = [p for p in profiles if p.get("type") == "BUSINESS"] if isinstance(profiles, list) else []
    if len(businesses) != 1:
        raise SystemExit("refused: expected exactly one BUSINESS profile")
    profile_id = businesses[0].get("id")
    recipient = _send("GET", "/{}/accounts/{}".format(API_VERSION, args.recipient_id), token)
    if str(recipient.get("profileId")) != str(profile_id) or recipient.get("active") is not True or recipient.get("currency") != args.target_currency:
        raise SystemExit("refused: recipient must be active, belong to the BUSINESS profile and use the target currency; Benchi must add or change recipients in the Wise UI")
    print("recipient: {} — {}".format(recipient.get("name", ""), recipient.get("longAccountSummary", "")))
    transfers = read_transfers(profile_id, token, args.recipient_id)
    completed = [r for r in transfers if r.get("status") == "outgoing_payment_sent"]
    if not completed:
        raise SystemExit("refused: no completed transfer to this recipient")
    completed.sort(key=lambda r: str(r.get("created") or r.get("createdTime") or ""), reverse=True)
    source_currency = args.source_currency or completed[0].get("sourceCurrency")
    if not source_currency:
        raise SystemExit("refused: source currency is unavailable")
    duplicate = next((r for r in transfers if any(ref.strip().casefold() == args.invoice.strip().casefold() for ref in reference_of(r))
                      and str(r.get("status", "")).casefold() not in FINAL_UNPAID), None)
    if duplicate:
        raise SystemExit("refused: invoice reference already has transfer {} ({})".format(duplicate.get("id"), duplicate.get("status")))
    today = datetime.date.today()
    if args.yes and planned < today:
        raise SystemExit("refused: planned date is in the past")
    if args.yes and planned > today + datetime.timedelta(days=14):
        raise SystemExit("refused: planned date is more than 14 days away")
    quote_body = {"sourceCurrency": source_currency, "targetCurrency": args.target_currency,
                  "sourceAmount": None, "targetAmount": float(amount), "targetAccount": args.recipient_id,
                  "preferredPayIn": "BALANCE"}
    body = transfer_body(args, source_currency)
    body["quoteUuid"] = "<quote UUID>"
    print("checks: BUSINESS profile; active recipient; completed recipient history; no duplicate invoice reference")
    print("planned date: {}; within 14 days; rate floats after 24–48 hours; unfunded transfers cancel after about 14 days".format(planned))
    print("quote request: {}".format(json.dumps(quote_body, sort_keys=True)))
    print("transfer request: {}".format(json.dumps(body, sort_keys=True)))
    if not args.yes:
        print("dry run: nothing sent")
        print("price: known only after the quote, on --yes")
        record_result("dry run")
        return 0
    try:
        quote = _send("POST", "/{}/profiles/{}/quotes".format(API_VERSION, profile_id), token, body=quote_body)
        option = next((o for o in quote.get("paymentOptions", []) if o.get("payIn") == "BALANCE"), {})
        if quote.get("targetAmountAllowed") is not True or Decimal(str(quote.get("targetAmount"))) != amount or quote.get("sourceCurrency") != source_currency or quote.get("targetCurrency") != args.target_currency:
            raise SystemExit("refused: quote amount or currencies do not match the request")
        if option.get("disabled"):
            print("BALANCE option disabled: {}".format((option.get("disabledReason") or {}).get("message", "reason unavailable")))
        body["quoteUuid"] = quote.get("id")
        requirements_body = {"targetAccount": args.recipient_id, "quote": quote.get("id"), "details": body["details"]}
        requirements = _send("POST", "/{}/transfer-requirements".format(API_VERSION), token, body=requirements_body)
        validate_requirements(requirements, body["details"])
        created = _send("POST", "/{}/transfers".format(API_VERSION), token, body=body)
    except SystemExit as err:
        if "missing approval" in str(err).casefold():
            raise SystemExit("a payment approval rule on the account blocks API transfers; Benchi must change this")
        record_result("failed")
        raise
    transfer_id = str(created.get("id", "")) if isinstance(created, dict) else ""
    if not transfer_id.isdigit():
        record_result("create response missing id")
        raise SystemExit("Wise answered without a transfer id; check transfers before rerunning")
    readback = _send("GET", "/{}/transfers/{}".format(API_VERSION, transfer_id), token)
    status = str(readback.get("status", "unknown"))
    same = (status == "incoming_payment_waiting" and str(readback.get("targetAccount")) == str(args.recipient_id)
            and any(ref.strip() == args.invoice.strip() for ref in reference_of(readback))
            and readback.get("targetCurrency") == args.target_currency
            and Decimal(str(readback.get("targetValue", readback.get("targetAmount", "-1")))) == amount)
    if not same:
        record_result("read-back mismatch", transfer_id, status)
        raise SystemExit("transfer {} read-back mismatch; do not retry or cancel".format(transfer_id))
    fee = option.get("fee", {}).get("total", "unknown")
    print("transfer id: {}; status: {}".format(transfer_id, status))
    print("source: {} {}; BALANCE fee: {}; target: {} {}".format(quote.get("sourceAmount"), source_currency, fee, amount, args.target_currency))
    expiration = readback.get("rateExpirationTime", quote.get("rateExpirationTime", "unknown"))
    print("rate held until {}, then it floats: check the amount when funding".format(expiration))
    created_time = readback.get("created") or readback.get("createdTime")
    try:
        cancel_day = (datetime.date.fromisoformat(created_time[:10]) + datetime.timedelta(days=14)).isoformat()
    except (TypeError, ValueError):
        cancel_day = "about 14 days after creation"
    print("Wise cancels this unfunded transfer about {}".format(cancel_day))
    print("funding: Benchi, in the Wise web UI")
    record_result("prepared", transfer_id, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
__WISE_PREPARE_TRANSFER_PY__
