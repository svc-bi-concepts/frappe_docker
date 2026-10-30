#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash
# The Python below reaches python3 on stdin, inside this one blessed file.
# av runs a Blessed Script as /dev/fd/N, and the mini's Python 3.9 reads a
# /dev/fd script from where its zip check left the shared offset: a script of
# more than a few bytes then runs empty and exits 0 (IT, 2026-10-14).
exec /usr/bin/python3 -I - "$@" <<'__BEXIO_CONTACT_BILL_PY__'
"""Create a supplier contact when needed and prepare a draft bill for an uploaded file.

The vendor is supplied by a private JSON file or identified by an existing name/UID.
The bill uses one line item, gross for ordinary VAT and net for reverse charge. Reads are completed before either approved write;
POST requests are never retried because an error may follow a successful store.

Authentication is the personal access token BEXIO_TOKEN injected by av. Dry run is
the default; --yes is required to create the contact and draft bill. Real run results
are appended to private/bexio-upload/last-result.txt without the token.

    ./create_contact_and_bill.py --vendor <private-vendor.json> --file-uuid <uuid> --nr <nr> --date <date> --total <amount> --account <account-no> --tax <code> --text <text>
    ./create_contact_and_bill.py --vendor-name <name> --file-uuid <uuid> --nr <nr> --date <date> --total <amount> --account <account-no> --tax <code> --text <text>

Standard library only, like client.py.
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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

BEXIO_DIR = os.environ.get("BEXIO_DIR") or "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/bexio"
sys.path.insert(0, BEXIO_DIR)

import client  # noqa: E402

REQUESTS = {
    ("GET", "/2.0/contact"),
    ("GET", "/2.0/country"),
    ("GET", "/3.0/users/me"),
    ("GET", "/4.0/purchase/bills"),
    ("GET", "/2.0/accounts"),
    ("GET", "/3.0/taxes"),
    ("POST", "/2.0/contact"),
    ("POST", "/4.0/purchase/bills"),
}
PAGE_SIZE = 500
BILL_STATUS = "DRAFT"
REVERSE_CHARGE_CODES = {"BZM77", "BZM81", "BZB77", "BZB81"}
PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
RESULT_FOLDER = "bexio-upload"
RESULT_FILE = "last-result.txt"
VENDOR_KEYS = {"name_1", "vat_number", "street_name", "house_number", "postcode", "city", "country", "mail"}
# Fields accepted by POST /2.0/contact, per https://docs.bexio.com/#tag/Contacts/operation/v2CreateContact.
CONTACT_CREATE_FIELDS = {
    "nr", "contact_type_id", "name_1", "name_2", "salutation_id", "salutation_form", "title_id",
    "birthday", "street_name", "house_number", "address_addition", "postcode", "city",
    "country_id", "mail", "mail_second", "phone_fixed", "phone_fixed_second", "phone_mobile",
    "fax", "url", "skype_name", "remarks", "language_id", "contact_group_ids",
    "contact_branch_ids", "user_id", "owner_id",
}


def record_result(line):
    """Append one result line without storing credentials."""
    folder = os.path.join(PRIVATE, RESULT_FOLDER)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, RESULT_FILE), "a") as f:
        f.write(line.rstrip("\n") + "\n")


class RefusedRequest(Exception):
    """A request outside the reviewed set. It is never sent."""


def _send(method, path, token, body=None, query=None):
    """Send one reviewed request; this is the only place a request leaves this script."""
    if (method, path) not in REQUESTS:
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
    req.add_header("User-Agent", "yardr-finance-bexio-bill/1.0")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise SystemExit("bexio refused {} {} (HTTP {}): {}".format(method, path, err.code, client._error_detail(err) or "no detail"))
    except urllib.error.URLError:
        raise SystemExit("could not reach bexio on {} {}; nothing more was sent".format(method, path))


def _paged_list(path, token):
    rows, offset = [], 0
    while True:
        page = _send("GET", path, token, query={"limit": PAGE_SIZE, "offset": offset})
        if not isinstance(page, list):
            raise SystemExit("bexio answered {} with something other than a list".format(path))
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += len(page)


def list_contacts(token):
    return _paged_list("/2.0/contact", token)


def list_accounts(token):
    return _paged_list("/2.0/accounts", token)


def list_taxes(token):
    return _paged_list("/3.0/taxes", token)


def list_bills(token, supplier_id=None):
    rows, page = [], 1
    while True:
        query = {"page": page, "limit": PAGE_SIZE}
        if supplier_id is not None:
            query["supplier_id"] = supplier_id
        body = _send("GET", "/4.0/purchase/bills", token, query=query)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise SystemExit("bexio answered the bill list without its rows")
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            return rows
        page += 1


def normalize_uid(value):
    match = re.search(r"(?i)\bCHE[\s.-]*(\d{3})[\s.-]?(\d{3})[\s.-]?(\d{3})(?!\d)", str(value or ""))
    return "".join(match.groups()) if match else ""


def contact_uid(contact):
    return normalize_uid(contact.get("remarks"))


def canonical_uid(value):
    if not re.fullmatch(r"(?i)\s*CHE[\s.-]*\d{3}[\s.-]?\d{3}[\s.-]?\d{3}(?:\s+MWST)?\s*", str(value or "")):
        raise SystemExit("refused vendor UID: expected CHE followed by nine digits")
    uid = normalize_uid(value)
    if not uid:
        raise SystemExit("refused vendor UID: expected CHE followed by nine digits")
    return "CHE-{}.{}.{}".format(uid[:3], uid[3:6], uid[6:])


def normalize_name(value):
    return " ".join(str(value or "").casefold().split())


def find_vendor(contacts, vendor):
    """Prefer exact UID, then exact normalized name; refuse ambiguous matches."""
    uid = normalize_uid(vendor.get("vat_number"))
    found = [c for c in contacts if uid and contact_uid(c) == uid]
    name = normalize_name(vendor.get("name_1"))
    named = [c for c in contacts if name and normalize_name(c.get("name_1")) == name]
    different_uid = [c for c in named if uid and contact_uid(c) and contact_uid(c) != uid]
    if different_uid:
        ids = {str(c.get("id")) for c in found + different_uid}
        raise SystemExit("name and UID point to different contact id(s) {}; check bexio before proceeding".format(
            ", ".join(sorted(ids))))
    if not found:
        found = named
    if len(found) > 1:
        raise SystemExit("{} contacts match the vendor ({}); resolve the duplicates in bexio first".format(
            len(found), ", ".join(str(c.get("id")) for c in found)))
    return found[0] if found else None


def check_vendor_path(path):
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not real.startswith(os.path.realpath(PRIVATE) + os.sep):
        raise SystemExit("refused {}: a vendor file must be under the private folder".format(path))
    return real


def load_vendor(path):
    real = check_vendor_path(path)
    try:
        with open(real, encoding="utf-8") as f:
            vendor = json.load(f)
    except (OSError, ValueError) as err:
        raise SystemExit("refused vendor file: {}".format(err))
    if not isinstance(vendor, dict):
        raise SystemExit("refused vendor file: expected a JSON object")
    unknown = set(vendor) - VENDOR_KEYS
    if unknown:
        raise SystemExit("refused vendor file: unknown keys {}".format(", ".join(sorted(unknown))))
    if not isinstance(vendor.get("name_1"), str) or not vendor["name_1"].strip():
        raise SystemExit("refused vendor file: name_1 is required")
    for key, value in vendor.items():
        if value is not None and not isinstance(value, str):
            raise SystemExit("refused vendor file: {} must be a string".format(key))
    vendor = dict(vendor)
    vendor["name_1"] = vendor["name_1"].strip()
    vendor["country"] = vendor.get("country", "CH")
    if not isinstance(vendor["country"], str) or not re.fullmatch(r"[A-Z]{2}", vendor["country"]):
        raise SystemExit("refused vendor file: country must be a two-letter upper-case ISO code")
    return vendor


def vendor_from_args(args):
    if args.vendor:
        if args.vendor_name or args.vendor_uid:
            raise SystemExit("refused: use --vendor or lookup arguments, not both")
        vendor = load_vendor(args.vendor)
        if vendor.get("vat_number"):
            canonical_uid(vendor["vat_number"])
        return vendor, True
    if not args.vendor_name and not args.vendor_uid:
        raise SystemExit("refused: pass exactly one of --vendor or --vendor-name/--vendor-uid")
    if args.vendor_uid:
        canonical_uid(args.vendor_uid)
    return {"name_1": args.vendor_name or "", "vat_number": args.vendor_uid}, False


def contact_body(vendor, owner_id, country_id):
    unknown_vendor_fields = set(vendor) - VENDOR_KEYS
    if unknown_vendor_fields:
        raise ValueError("vendor contains unsupported fields: {}".format(", ".join(sorted(unknown_vendor_fields))))
    fields = {key: value for key, value in vendor.items()
              if key in CONTACT_CREATE_FIELDS and key in VENDOR_KEYS - {"country", "vat_number"} and value is not None}
    if vendor.get("vat_number"):
        fields["remarks"] = "UID " + canonical_uid(vendor["vat_number"])
    fields.update(contact_type_id=1, country_id=country_id, user_id=owner_id, owner_id=owner_id)
    unknown = set(fields) - CONTACT_CREATE_FIELDS
    if unknown:
        raise ValueError("contact body contains undocumented fields: {}".format(", ".join(sorted(unknown))))
    return fields


def resolve_account(accounts, account_no):
    matches = [a for a in accounts if str(a.get("account_no")) == account_no]
    if not matches:
        raise SystemExit("refused: unknown account {}".format(account_no))
    if len(matches) != 1:
        raise SystemExit("refused: account {} is ambiguous".format(account_no))
    account = matches[0]
    if not account.get("is_active"):
        raise SystemExit("refused: account {} is inactive".format(account_no))
    if account.get("is_locked"):
        raise SystemExit("refused: account {} is locked".format(account_no))
    if account.get("account_type") == 1:
        raise SystemExit("refused: account {} is revenue; ask Benchi which expense account to use".format(account_no))
    return account


def resolve_tax(taxes, code):
    matches = [tax for tax in taxes if tax.get("code") == code]
    if not matches:
        raise SystemExit("refused: unknown tax code {}".format(code))
    if len(matches) != 1:
        raise SystemExit("refused: tax code {} is ambiguous".format(code))
    tax = matches[0]
    if not tax.get("is_active"):
        raise SystemExit("refused: tax code {} is inactive".format(code))
    if code not in REVERSE_CHARGE_CODES and not str(tax.get("type", "")).startswith("pre_"):
        raise SystemExit("{} is a sales VAT code, not input VAT; ask Benchi which code".format(code))
    return tax


def contact_address(vendor):
    address_line = " ".join(part for part in (vendor.get("street_name"), vendor.get("house_number")) if part)
    address = {
        "lastname_company": vendor["name_1"],
        "type": "PRIVATE" if vendor.get("contact_type_id") == 2 else "COMPANY",
    }
    # Optional address fields have minLength 1 in the create schema: omit missing ones.
    for key, value in (("address_line", address_line), ("postcode", vendor.get("postcode")),
                       ("city", vendor.get("city")), ("country_code", vendor.get("country"))):
        if value:
            address[key] = value
    return address


def bill_body(supplier_id, partner_id, vendor, file_uuid, nr, date, due_date, total, text, currency, account, tax):
    return {
        "supplier_id": supplier_id,
        "vendor_ref": nr,
        "title": text,
        "contact_partner_id": partner_id,
        "bill_date": date,
        "due_date": due_date,
        "amount_man": float(total),
        "manual_amount": True,
        "currency_code": currency,
        "item_net": tax["code"] in REVERSE_CHARGE_CODES,
        "attachment_ids": [file_uuid],
        "address": contact_address(vendor),
        "line_items": [{"position": 0, "amount": float(total), "booking_account_id": account["id"], "tax_id": tax["id"], "title": text}],
        "discounts": [],
    }


def checked_total(text):
    try:
        total = Decimal(text)
    except InvalidOperation:
        raise SystemExit("refused total {!r}: not a number".format(text))
    if not total.is_finite() or total <= 0 or total != total.quantize(Decimal("0.01")):
        raise SystemExit("refused total {!r}: a positive amount with at most two decimals".format(text))
    return total.quantize(Decimal("0.01"))


def checked_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise SystemExit("refused date {!r}: use YYYY-MM-DD".format(text))


def main(argv):
    parser = argparse.ArgumentParser(description="Create a supplier contact if needed and a draft bill.")
    parser.add_argument("--vendor", help="vendor JSON file under private/")
    parser.add_argument("--vendor-name", help="exact existing contact name (lookup only)")
    parser.add_argument("--vendor-uid", help="existing contact UID (lookup only)")
    parser.add_argument("--file-uuid", required=True, help="uuid from upload_bill.py's result line")
    parser.add_argument("--nr", required=True, help="vendor invoice number")
    parser.add_argument("--date", required=True, help="invoice date, YYYY-MM-DD")
    parser.add_argument("--total", required=True,
                        help="invoice total; for reverse charge, the invoice amount is net and the whole bill total")
    parser.add_argument("--account", required=True, help="expense account number")
    parser.add_argument("--tax", required=True, help="input VAT code")
    parser.add_argument("--currency", default="CHF", help="three-letter upper-case currency (default: CHF)")
    parser.add_argument("--exchange-rate", help="CHF per currency unit; required for non-CHF bills")
    parser.add_argument("--due-date", help="due date, YYYY-MM-DD (default: bill date plus 30 days)")
    parser.add_argument("--text", required=True, help="bill and line item title")
    parser.add_argument("--yes", action="store_true", help="create for real (default: dry run)")
    args = parser.parse_args(argv)

    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is not set; run through av inject (see finance/bexio/README.md)")
    vendor, can_create = vendor_from_args(args)
    if not args.file_uuid.strip() or not args.nr.strip() or not args.text.strip():
        raise SystemExit("refused: file uuid, invoice number and text must not be empty")
    if not re.fullmatch(r"[A-Z]{3}", args.currency):
        raise SystemExit("refused currency: use three upper-case letters")
    total = checked_total(args.total)
    exchange_rate = None
    if args.currency != "CHF":
        try:
            exchange_rate = Decimal(args.exchange_rate or "")
        except InvalidOperation:
            raise SystemExit("refused: non-CHF bills require --exchange-rate (CHF per currency unit)")
        if not exchange_rate.is_finite() or exchange_rate <= 0 or exchange_rate >= 100000 or exchange_rate.as_tuple().exponent < -10:
            raise SystemExit("refused exchange rate: use a positive rate with at most 5 integer and 10 decimal digits")
    elif args.exchange_rate is not None:
        raise SystemExit("refused: --exchange-rate is only used for non-CHF bills")
    bill_date = checked_date(args.date)
    due_date = checked_date(args.due_date) if args.due_date else bill_date + datetime.timedelta(days=30)
    if due_date < bill_date:
        raise SystemExit("refused due date: it must be on or after the bill date")

    # Finish all reads before any write, so a refusal cannot leave a partial contact/bill.
    contacts = list_contacts(token)
    bills = list_bills(token)
    accounts = list_accounts(token)
    taxes = list_taxes(token)
    account = resolve_account(accounts, args.account)
    tax = resolve_tax(taxes, args.tax)
    existing = find_vendor(contacts, vendor)
    contact_plan = None
    if existing:
        supplier_id = existing["id"]
        partner_id = existing.get("contact_partner_id") or supplier_id
        # A UID match can have a different current name/address from the input file.
        vendor_for_bill = dict(existing)
        vendor_for_bill["country"] = existing.get("country_code") or existing.get("country")
        if not vendor_for_bill["country"] and existing.get("country_id") is not None:
            countries = _send("GET", "/2.0/country", token)
            country = [c for c in countries if c.get("id") == existing["country_id"]]
            if len(country) != 1:
                raise SystemExit("bexio has no single country for the existing contact")
            vendor_for_bill["country"] = country[0]["iso_3166_alpha2"]
        print("contact: found '{}' as bexio contact id {}; not created".format(existing.get("name_1"), supplier_id))
    elif can_create:
        country = [c for c in _send("GET", "/2.0/country", token) if c.get("iso_3166_alpha2") == vendor["country"]]
        if len(country) != 1:
            raise SystemExit("bexio has no single country for {}".format(vendor["country"]))
        me = _send("GET", "/3.0/users/me", token)
        contact_plan = contact_body(vendor, me["id"], country[0]["id"])
        supplier_id = None
        partner_id = None
        vendor_for_bill = vendor
        print("contact: no exact UID or name match; would create:")
        print(json.dumps(contact_plan, ensure_ascii=False, indent=2))
    else:
        raise SystemExit("no contact; pass --vendor <file> to create one")

    duplicate_refs = [b for b in bills if str(b.get("vendor_ref") or "") == args.nr]
    if supplier_id is not None:
        # The list response has no supplier_id: ask the API to filter it instead.
        same_supplier = [b for b in list_bills(token, supplier_id=supplier_id)
                         if str(b.get("vendor_ref") or "") == args.nr]
        if same_supplier:
            raise SystemExit("refused: vendor_ref {} already exists for this supplier; nothing was created".format(args.nr))
    if duplicate_refs:
        print("warning: vendor_ref {} exists for another supplier".format(args.nr))

    due_iso = due_date.isoformat()
    print("account: {} — {} (type {})".format(account.get("account_no"), account.get("name"), account.get("account_type")))
    print("tax: {} — {} ({}%)".format(tax.get("code"), tax.get("display_name"), tax.get("value")))
    print("due date: {}".format(due_iso))
    bill_plan = bill_body(supplier_id, partner_id, vendor_for_bill, args.file_uuid, args.nr, bill_date.isoformat(), due_iso, total, args.text, args.currency, account, tax)
    if exchange_rate is not None:
        bill_plan["exchange_rate"] = float(exchange_rate)
        bill_plan["base_currency_amount"] = float((total * exchange_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    print("bill: would create as {}:".format(BILL_STATUS))
    print("amount basis: net, reverse charge (Bezugsteuer)" if tax["code"] in REVERSE_CHARGE_CODES
          else "amount basis: gross")
    print(json.dumps(bill_plan, ensure_ascii=False, indent=2))
    if not args.yes:
        print("dry run: nothing created; add --yes to create")
        return

    if contact_plan is not None:
        created = _send("POST", "/2.0/contact", token, body=contact_plan)
        if not isinstance(created, dict) or created.get("id") is None:
            raise SystemExit("bexio answered the contact without an id; check contacts before running again")
        supplier_id = created["id"]
        partner_id = created.get("contact_partner_id") or supplier_id
        line = "contact: created bexio contact id {} ({})".format(supplier_id, vendor["name_1"])
        print(line)
        record_result(line)
        bill_plan["supplier_id"] = supplier_id
        bill_plan["contact_partner_id"] = partner_id
    bill = _send("POST", "/4.0/purchase/bills", token, body=bill_plan)
    if not isinstance(bill, dict) or bill.get("id") is None:
        raise SystemExit("bexio answered the bill without an id; check bills before running again")
    line = "bill: created bexio bill id {} vendor_ref {} supplier id {} as {}".format(bill["id"], args.nr, supplier_id, BILL_STATUS)
    print(line)
    record_result(line)


if __name__ == "__main__":
    main(sys.argv[1:])
__BEXIO_CONTACT_BILL_PY__
