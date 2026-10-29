#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash
# The Python below reaches python3 on stdin, inside this one blessed file.
# av runs a Blessed Script as /dev/fd/N, and the mini's Python 3.9 reads a
# /dev/fd script from where its zip check left the shared offset: a script of
# more than a few bytes then runs empty and exits 0 (IT, 2026-10-14).
exec /usr/bin/python3 -I - "$@" <<'__BEXIO_CONTACT_BILL_PY__'
"""Create a supplier contact when needed and prepare a draft bill for an uploaded file.

The vendor is supplied by a private JSON file or identified by an existing name/UID.
The bill uses one gross line item. Reads are completed before either approved write;
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
from decimal import Decimal, InvalidOperation

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
PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"
RESULT_FOLDER = "bexio-upload"
RESULT_FILE = "last-result.txt"
VENDOR_KEYS = {"name_1", "vat_number", "street_name", "house_number", "postcode", "city", "country", "mail"}


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


def list_bills(token):
    rows, page = [], 1
    while True:
        body = _send("GET", "/4.0/purchase/bills", token, query={"page": page, "page_size": PAGE_SIZE})
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise SystemExit("bexio answered the bill list without its rows")
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            return rows
        page += 1


def normalize_uid(value):
    return "".join(re.findall(r"\d", str(value or "")))


def normalize_name(value):
    return " ".join(str(value or "").casefold().split())


def find_vendor(contacts, vendor):
    """Prefer exact UID, then exact normalized name; refuse ambiguous matches."""
    uid = normalize_uid(vendor.get("vat_number"))
    found = [c for c in contacts if uid and normalize_uid(c.get("vat_number")) == uid]
    if not found:
        name = normalize_name(vendor.get("name_1"))
        found = [c for c in contacts if name and normalize_name(c.get("name_1")) == name]
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
        return load_vendor(args.vendor), True
    if not args.vendor_name and not args.vendor_uid:
        raise SystemExit("refused: pass exactly one of --vendor or --vendor-name/--vendor-uid")
    return {"name_1": args.vendor_name or "", "vat_number": args.vendor_uid}, False


def contact_body(vendor, owner_id, country_id):
    fields = {key: value for key, value in vendor.items() if key in VENDOR_KEYS - {"country"} and value is not None}
    fields.update(contact_type_id=1, country_id=country_id, user_id=owner_id, owner_id=owner_id)
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
    if not str(tax.get("type", "")).startswith("pre_"):
        raise SystemExit("{} is a sales VAT code, not input VAT; ask Benchi which code".format(code))
    return tax


def contact_address(vendor):
    address_line = " ".join(part for part in (vendor.get("street_name"), vendor.get("house_number")) if part)
    return {
        "lastname_company": vendor["name_1"],
        "address_line": address_line,
        "postcode": vendor.get("postcode") or "",
        "city": vendor.get("city") or "",
        "country_code": vendor.get("country") or "CH",
    }


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
        "item_net": False,
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
    parser.add_argument("--total", required=True, help="gross invoice total")
    parser.add_argument("--account", required=True, help="expense account number")
    parser.add_argument("--tax", required=True, help="input VAT code")
    parser.add_argument("--currency", default="CHF", help="three-letter upper-case currency (default: CHF)")
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
        partner_id = existing.get("contact_partner_id", supplier_id)
        vendor_for_bill = dict(vendor)
        for key in ("name_1", "street_name", "house_number", "postcode", "city", "country"):
            if not vendor_for_bill.get(key) and existing.get(key):
                vendor_for_bill[key] = existing[key]
        if not vendor_for_bill.get("country") and existing.get("country_code"):
            vendor_for_bill["country"] = existing["country_code"]
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
        same_supplier = [b for b in duplicate_refs if str(b.get("supplier_id")) == str(supplier_id)]
        if same_supplier:
            raise SystemExit("refused: vendor_ref {} already exists for this supplier; nothing was created".format(args.nr))
    if duplicate_refs:
        print("warning: vendor_ref {} exists for another supplier".format(args.nr))

    due_iso = due_date.isoformat()
    print("account: {} — {} (type {})".format(account.get("account_no"), account.get("name"), account.get("account_type")))
    print("tax: {} — {} ({}%)".format(tax.get("code"), tax.get("display_name"), tax.get("value")))
    print("due date: {}".format(due_iso))
    bill_plan = bill_body(supplier_id, partner_id, vendor_for_bill, args.file_uuid, args.nr, bill_date.isoformat(), due_iso, total, args.text, args.currency, account, tax)
    print("bill: would create as {}:".format(BILL_STATUS))
    print(json.dumps(bill_plan, ensure_ascii=False, indent=2))
    if not args.yes:
        print("dry run: nothing created; add --yes to create")
        return

    if contact_plan is not None:
        created = _send("POST", "/2.0/contact", token, body=contact_plan)
        if not isinstance(created, dict) or created.get("id") is None:
            raise SystemExit("bexio answered the contact without an id; check contacts before running again")
        supplier_id = created["id"]
        partner_id = created.get("contact_partner_id", supplier_id)
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
