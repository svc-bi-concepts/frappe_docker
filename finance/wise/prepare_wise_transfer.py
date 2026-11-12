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
    ("GET", "/2026Q4/accounts"),
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
    path = re.sub(r"(?<=/profiles/)[0-9]+(?=/quotes\Z)", "{id}", path)
    path = re.sub(r"(?<=/accounts/)[0-9]+\Z", "{id}", path)
    path = re.sub(r"(?<=/transfers/)[0-9]+\Z", "{id}", path)
    return method, path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never carry the Authorization header to another host.
        return None


OPENER = urllib.request.build_opener(NoRedirect())


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
        with OPENER.open(req, timeout=60) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as err:
        codes = []
        approval = False
        try:
            error_text = err.read().decode("utf-8", errors="replace")
            approval = "missing approval" in error_text.casefold()
            payload = json.loads(error_text)
            errors = payload.get("errors", [])
            codes = [str(e["code"]) for e in errors if isinstance(e, dict) and e.get("code")]
        except (ValueError, AttributeError, TypeError):
            pass
        message = "Wise refused {} {} (HTTP {})".format(method, path, err.code)
        if codes:
            message += " ({})".format(", ".join(codes))
        if approval:
            message += "; a payment approval rule on the account blocks API transfers; Benchi must change this"
        raise SystemExit(message.replace(token, "[redacted]")) from None
    except (OSError, ValueError) as err:
        raise SystemExit("no usable answer from Wise on {} {} ({}); no further request was sent".format(method, path, type(err).__name__)) from None


def record_result(outcome, transfer_id="", status=""):
    folder = os.path.join(PRIVATE, "wise")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "last-result.txt"), "a", encoding="utf-8") as result:
        result.write("outcome: {}; transfer_id: {}; status: {}\n".format(outcome, transfer_id, status))


def amount_value(raw):
    try:
        value = Decimal(raw)
        valid = (value.is_finite() and value > 0 and value == value.quantize(Decimal("0.01"))
                 and Decimal(str(float(value))) == value)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise SystemExit("refused: amount must be a positive number with at most two decimals, representable without rounding")
    return value


def iter_transfer_rows(profile_id, token):
    offset = 0
    while True:
        page = _send("GET", "/{}/transfers".format(API_VERSION), token,
                     query={"profile": profile_id, "limit": 100, "offset": offset})
        if not isinstance(page, list):
            raise SystemExit("Wise answered the transfer list without rows")
        yield from page
        if len(page) < 100:
            return
        offset += len(page)


def read_transfers(profile_id, token, recipient_id):
    return [row for row in iter_transfer_rows(profile_id, token)
            if str(row.get("targetAccount")) == str(recipient_id)]


def normalized_name(value):
    return " ".join(str(value or "").casefold().split())


def recipient_name(recipient):
    value = recipient.get("name", "")
    if isinstance(value, dict):
        value = value.get("fullName", "")
    return str(value or "")


def iter_account_rows(profile_id, token):
    # The profile's account list holds recipients that were never paid too.
    # It may answer as a plain list or as a page with a seek position.
    query = {"profileId": profile_id, "size": 100}
    seen = set()
    while True:
        page = _send("GET", "/{}/accounts".format(API_VERSION), token, query=query)
        if isinstance(page, list):
            yield from page
            return
        if not isinstance(page, dict) or not isinstance(page.get("content"), list):
            raise SystemExit("Wise answered the account list without rows")
        yield from page["content"]
        following = page.get("seekPositionForNext")
        if not page["content"] or following is None or str(following) in seen:
            return
        seen.add(str(following))
        query = dict(query, seekPosition=following)


def profile_accounts(profile_id, token):
    accounts = {}
    for row in iter_account_rows(profile_id, token):
        if (isinstance(row, dict) and str(row.get("profileId")) == str(profile_id)
                and re.fullmatch(r"[0-9]+", str(row.get("id")))):
            accounts[int(row["id"])] = row
    return accounts


def normalized_iban(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def recipient_iban(recipient):
    details = recipient.get("details")
    return details.get("iban") if isinstance(details, dict) else None


UUID_KEYS = ("uuid", "publicid")


def holds_uuid(row, wanted):
    return any(str(key).casefold() in UUID_KEYS and isinstance(value, str)
               and value.strip().casefold() == wanted for key, value in row.items())


def resolve_recipient_uuid(profile_id, token, recipient_uuid):
    # The web app shows a UUID in the recipient URL; transfers need the
    # numeric account id. Only an exact match of a top-level uuid or publicId
    # field in this profile's list counts.
    matches = [account_id for account_id, row in profile_accounts(profile_id, token).items()
               if holds_uuid(row, recipient_uuid)]
    if len(matches) != 1:
        raise SystemExit("refused: recipient UUID {} matches {} accounts of the BUSINESS profile; "
                         "use --find-recipient NAME or --find-iban IBAN for the numeric id".format(
                             recipient_uuid, len(matches)))
    print("recipient UUID {} is account id {}".format(recipient_uuid, matches[0]))
    return matches[0]


def recipient_id_value(raw):
    raw = raw.strip()
    if re.fullmatch(r"[0-9]+", raw):
        return int(raw)
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw):
        return raw.casefold()
    raise SystemExit("refused: --recipient-id must be the numeric account id or the recipient UUID "
                     "from the Wise web app; use --find-recipient NAME or --find-iban IBAN")


def find_recipients(profile_id, token, name=None, iban=None):
    completed = {}
    for row in iter_transfer_rows(profile_id, token):
        recipient_id = row.get("targetAccount")
        if (row.get("status") == "outgoing_payment_sent"
                and re.fullmatch(r"[0-9]+", str(recipient_id))):
            recipient_id = int(recipient_id)
            completed[recipient_id] = completed.get(recipient_id, 0) + 1

    accounts = profile_accounts(profile_id, token)
    unreadable = 0
    # A paid recipient missing from the list (an inactive one) is read by id.
    for recipient_id in sorted(set(completed) - set(accounts)):
        try:
            recipient = _send("GET", "/{}/accounts/{}".format(API_VERSION, recipient_id), token)
        except SystemExit:
            unreadable += 1
            continue
        if isinstance(recipient, dict) and str(recipient.get("profileId")) == str(profile_id):
            accounts[recipient_id] = recipient

    matches = []
    for recipient_id in sorted(accounts):
        recipient = accounts[recipient_id]
        if name is not None and normalized_name(name) not in normalized_name(recipient_name(recipient)):
            continue
        if iban is not None and (not normalized_iban(recipient_iban(recipient))
                                 or normalized_iban(recipient_iban(recipient)) != normalized_iban(iban)):
            continue
        matches.append((recipient, completed.get(recipient_id, 0)))
    return matches, len(accounts), len(completed), unreadable


def business_profile(token):
    profiles = _send("GET", "/{}/profiles".format(API_VERSION), token)
    businesses = [p for p in profiles if p.get("type") == "BUSINESS"] if isinstance(profiles, list) else []
    if len(businesses) != 1:
        raise SystemExit("refused: expected exactly one BUSINESS profile")
    return businesses[0].get("id")


def reference_of(row):
    return [str(value) for value in (row.get("reference"), (row.get("details") or {}).get("reference")) if value]


def transaction_id(recipient_id, invoice, attempt):
    name = "{}:{}".format(recipient_id, invoice.strip())
    if attempt > 1:
        name += ":{}".format(attempt)
    return str(uuid.uuid5(NAMESPACE, name))


def transfer_body(args):
    details = {"reference": args.invoice.strip()}
    details.update(dict(args.detail))
    return {"targetAccount": args.recipient_id, "quoteUuid": "<quote UUID>",
            "customerTransactionId": transaction_id(args.recipient_id, args.invoice, args.attempt),
            "details": details}


def validate_requirements(answer, details):
    if not isinstance(answer, list):
        raise SystemExit("refused: Wise answered requirements without a field list")
    for requirement in answer:
        for field in requirement.get("fields", []):
            for item in field.get("group", []):
                key = item.get("key")
                if item.get("required") and not str(details.get(key) or "").strip():
                    raise SystemExit("refused: required field {} is empty; supply it with --detail".format(key))
                if key == "reference":
                    value = details["reference"]
                    if item.get("maxLength") is not None and len(value) > item["maxLength"]:
                        raise SystemExit("refused: reference exceeds Wise maxLength; do not shorten it")
                    if item.get("minLength") is not None and len(value) < item["minLength"]:
                        raise SystemExit("refused: reference is below Wise minLength")
                    regexp = item.get("validationRegexp")
                    if regexp and not re.fullmatch(regexp, value):
                        raise SystemExit("refused: reference does not match Wise validationRegexp")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find Wise recipients or prepare one unfunded transfer.")
    parser.add_argument("--find-recipient")
    parser.add_argument("--find-iban")
    parser.add_argument("--recipient-id")
    parser.add_argument("--invoice")
    parser.add_argument("--amount")
    parser.add_argument("--target-currency")
    parser.add_argument("--source-currency")
    parser.add_argument("--first-payment", action="store_true")
    parser.add_argument("--expect-iban")
    parser.add_argument("--expect-name")
    parser.add_argument("--planned-date")
    parser.add_argument("--detail", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    search = args.find_recipient is not None or args.find_iban is not None
    if search:
        if args.find_recipient is not None and not args.find_recipient.strip():
            raise SystemExit("refused: --find-recipient name must not be empty")
        if args.find_iban is not None and not normalized_iban(args.find_iban):
            raise SystemExit("refused: --find-iban IBAN must not be empty")
        if (args.recipient_id is not None or args.invoice is not None or args.amount is not None
                or args.target_currency is not None or args.planned_date is not None
                or args.source_currency is not None or args.first_payment or args.expect_iban is not None
                or args.expect_name is not None or args.detail or args.attempt is not None or args.yes):
            raise SystemExit("refused: --find-recipient and --find-iban cannot be combined with transfer arguments")
    else:
        for option in ("--recipient-id", "--invoice", "--amount", "--target-currency", "--planned-date"):
            if getattr(args, option[2:].replace("-", "_")) is None:
                raise SystemExit("refused: {} is required in transfer mode".format(option))
        if args.attempt is None:
            args.attempt = 1
        if args.first_payment:
            for option in ("--expect-iban", "--expect-name", "--source-currency"):
                if not getattr(args, option[2:].replace("-", "_")):
                    raise SystemExit("refused: {} is required with --first-payment".format(option))
        elif args.expect_iban is not None or args.expect_name is not None:
            raise SystemExit("refused: --expect-iban and --expect-name require --first-payment")
        args.recipient_id = recipient_id_value(args.recipient_id)
    token = os.environ.get("FINANCE_WISE_TRANSFER_TOKEN")
    if not token:
        raise SystemExit("FINANCE_WISE_TRANSFER_TOKEN is not set; run through av inject")
    if search:
        profile_id = business_profile(token)
        matches, checked, paid, unreadable = find_recipients(
            profile_id, token, args.find_recipient, args.find_iban)
        for recipient, count in matches:
            active = "yes" if recipient.get("active") is True else "no"
            print("id: {}; name: {}; iban: {}; currency: {}; active: {}; {}; completed transfers: {}".format(
                recipient.get("id", ""), recipient_name(recipient), recipient_iban(recipient) or "none",
                recipient.get("currency", ""), active, recipient.get("longAccountSummary", ""), count))
            if count == 0:
                print("  never paid: use --first-payment with --expect-iban and --expect-name")
        if not matches:
            print("no recipient matches")
        print("recipients checked: {} ({} with completed transfers), unreadable: {}".format(checked, paid, unreadable))
        if unreadable:
            print("unreadable recipients were not matched")
        return 0
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
    profile_id = business_profile(token)
    if isinstance(args.recipient_id, str):
        args.recipient_id = resolve_recipient_uuid(profile_id, token, args.recipient_id)
    recipient = _send("GET", "/{}/accounts/{}".format(API_VERSION, args.recipient_id), token)
    if str(recipient.get("profileId")) != str(profile_id) or recipient.get("active") is not True or recipient.get("currency") != args.target_currency:
        raise SystemExit("refused: recipient must be active, belong to the BUSINESS profile and use the target currency; Benchi must add or change recipients in the Wise UI")
    name = recipient.get("name", "")
    if isinstance(name, dict):
        name = name.get("fullName", "")
    print("recipient: {} — {}".format(name, recipient.get("longAccountSummary", "")))
    if args.first_payment:
        actual_iban = recipient_iban(recipient)
        actual_name = recipient_name(recipient)
        if (not normalized_iban(actual_iban) or normalized_iban(actual_iban) != normalized_iban(args.expect_iban)
                or not normalized_name(actual_name)
                or normalized_name(actual_name) != normalized_name(args.expect_name)):
            raise SystemExit("refused: first-payment recipient identity mismatch; IBAN Wise={!r}, expected={!r}; name Wise={!r}, expected={!r}".format(
                actual_iban, args.expect_iban, actual_name, args.expect_name))
    transfers = read_transfers(profile_id, token, args.recipient_id)
    completed = [r for r in transfers if r.get("status") == "outgoing_payment_sent"]
    if args.first_payment and completed:
        raise SystemExit("refused: recipient already has a completed transfer; use normal mode")
    if not args.first_payment and not completed:
        raise SystemExit("refused: no completed transfer to this recipient")
    completed.sort(key=lambda r: str(r.get("created") or r.get("createdTime") or ""), reverse=True)
    source_currency = args.source_currency if args.first_payment else args.source_currency or completed[0].get("sourceCurrency")
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
    body = transfer_body(args)
    body["quoteUuid"] = "<quote UUID>"
    history_check = "no completed recipient history (first payment)" if args.first_payment else "completed recipient history"
    print("checks: BUSINESS profile; active recipient; {}; identity matched; no duplicate invoice reference".format(history_check) if args.first_payment else "checks: BUSINESS profile; active recipient; {}; no duplicate invoice reference".format(history_check))
    print("planned date: {}; --yes requires today through today + 14 days; rate floats after 24–48 hours; unfunded transfers cancel after about 14 days".format(planned))
    print("quote request: {}".format(json.dumps(quote_body, sort_keys=True)))
    print("transfer request: {}".format(json.dumps(body, sort_keys=True)))
    if not args.yes:
        print("dry run: nothing sent")
        print("price: known only after the quote, on --yes")
        record_result("dry run")
        return 0
    try:
        quote = _send("POST", "/{}/profiles/{}/quotes".format(API_VERSION, profile_id), token, body=quote_body)
        if not isinstance(quote, dict):
            raise SystemExit("refused: quote response is not an object")
        option = next((o for o in (quote.get("paymentOptions") or []) if o.get("payIn") == "BALANCE"), {})
        try:
            quoted_amount = Decimal(str(quote.get("targetAmount")))
        except InvalidOperation:
            quoted_amount = Decimal("-1")
        if quote.get("targetAmountAllowed") is not True or not quoted_amount.is_finite() or quoted_amount != amount or quote.get("sourceCurrency") != source_currency or quote.get("targetCurrency") != args.target_currency:
            raise SystemExit("refused: quote amount or currencies do not match the request")
        if option.get("disabled"):
            print("BALANCE option disabled: {}".format((option.get("disabledReason") or {}).get("message", "reason unavailable")))
        body["quoteUuid"] = quote.get("id")
        if not body["quoteUuid"]:
            raise SystemExit("refused: quote has no id")
        requirements = _send("POST", "/{}/transfer-requirements".format(API_VERSION), token, body=body)
        validate_requirements(requirements, body["details"])
        created = _send("POST", "/{}/transfers".format(API_VERSION), token, body=body)
    except SystemExit:
        record_result("failed")
        raise
    transfer_id = str(created.get("id", "")) if isinstance(created, dict) else ""
    if not transfer_id.isdigit():
        record_result("create response missing id")
        raise SystemExit("Wise answered without a transfer id; check transfers before rerunning")
    print("transfer id: {}".format(transfer_id))
    try:
        readback = _send("GET", "/{}/transfers/{}".format(API_VERSION, transfer_id), token)
    except SystemExit:
        record_result("read-back failed", transfer_id)
        raise
    if not isinstance(readback, dict):
        record_result("read-back mismatch", transfer_id)
        raise SystemExit("transfer {} read-back mismatch; check transfers before rerunning".format(transfer_id))
    status = str(readback.get("status", "unknown"))
    try:
        target_amount = Decimal(str(readback.get("targetValue", readback.get("targetAmount"))))
    except InvalidOperation:
        target_amount = Decimal("-1")
    same = (status == "incoming_payment_waiting" and str(readback.get("targetAccount")) == str(args.recipient_id)
            and bool(reference_of(readback))
            and all(ref.strip() == args.invoice.strip() for ref in reference_of(readback))
            and readback.get("targetCurrency") == args.target_currency
            and target_amount.is_finite() and target_amount == amount)
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
