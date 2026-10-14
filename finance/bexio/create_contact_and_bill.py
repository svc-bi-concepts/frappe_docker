#!/usr/local/bin/av inject +BEXIO_TOKEN -- /usr/bin/python3 -I
"""Create a vendor contact in bexio and prepare a bill from the file already in its inbox.

The two writes after upload_bill.py: the contact (only when no contact of that name exists)
and the bill, with the uploaded file linked, the invoice number, date and total, and a booking
text. No line items: the bill is a draft with its header and total, and nothing is booked.
The contact is looked up by name first and reported either way.

Requests are the reviewed set below and nothing else: GET reads (contacts, countries, the
user, the bills to check the invoice number is new), POST /2.0/contact, POST /4.0/purchase/bills.
Reads never write. A POST is never retried: an answer that failed may still have stored the
record, so a second send could make a duplicate.

It authenticates with the personal access token only: BEXIO_TOKEN, injected by av (the
shebang above; not the OAuth login). A dry run is the default: it reads what it needs,
prints the contact and bill JSON it would create, and writes nothing; a real run needs
--yes. av inject drops the child's stdout (automic-vault#390), so every real run also
appends its result lines -- the contact id and the bill id, never the token -- to
private/bexio-upload/last-result.txt.

    ./create_contact_and_bill.py --file-uuid <uuid> --nr <nr> --date <YYYY-MM-DD> --total <amount>
    ./create_contact_and_bill.py --yes ...

Standard library only, like client.py.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client  # noqa: E402

# The vendor, from invoice 926672 (the branch it bills from, Dietikon), not vebego.ch's Impressum
# (Hauptsitz Zürich): the contact in the books matches the creditor on the invoice and QR-bill.
# Same legal entity (UID CHE-105.953.190). No phone or fax: the invoice does not print one.
# bexio's contact_type_id 1 is a company (import_master.py reads it the same way).
VENDOR_NAME = "Vebego AG"
VENDOR_SEARCH = "vebego"  # matched case-insensitively against name_1 of every contact
VENDOR_COUNTRY = "CH"
VENDOR_FIELDS = {
    "contact_type_id": 1,
    "name_1": VENDOR_NAME,
    "street_name": "Kanalstrasse",
    "house_number": "6",
    "postcode": "8953",
    "city": "Dietikon",
    "mail": "zuerich@vebego.ch",
    "vat_number": "CHE-105.953.190",
}

# The one request each path makes, checked on every send. Anything else is refused and never sent.
REQUESTS = {
    ("GET", "/2.0/contact"),  # contacts, paged by limit and offset: the search by name
    ("GET", "/2.0/country"),  # the country id of CH
    ("GET", "/3.0/users/me"),  # the owner of the new contact
    ("GET", "/4.0/purchase/bills"),  # bills, paged by page and page_size: the invoice number must be new
    ("POST", "/2.0/contact"),
    ("POST", "/4.0/purchase/bills"),
}

PAGE_SIZE = 500
BILL_STATUS = "DRAFT"  # a draft: prepared, not booked
DEFAULT_TEXT = "Vebego AG, Reinigung nach Anlass September 2026"

PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"

# av inject drops the child's stdout (automic-vault#390): a result that matters also goes
# here, one line per created record, appended, so the last line is the most recent run.
RESULT_FOLDER = "bexio-upload"
RESULT_FILE = "last-result.txt"


def record_result(line):
    """Append one result line to private/bexio-upload/last-result.txt. Never the token."""
    folder = os.path.join(PRIVATE, RESULT_FOLDER)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, RESULT_FILE), "a") as f:
        f.write(line.rstrip("\n") + "\n")


class RefusedRequest(Exception):
    """A request outside the reviewed set. It is never sent."""


def _send(method, path, token, body=None, query=None):
    """Send one reviewed request and return the decoded answer. The only place a request leaves this script.

    The token goes only into the header. An error answer stops the script with its status
    and the start of bexio's reason, never the token.
    """
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


def list_contacts(token):
    """Every contact, paged by offset: bexio's contact list is not one page."""
    rows, offset = [], 0
    while True:
        page = _send("GET", "/2.0/contact", token, query={"limit": PAGE_SIZE, "offset": offset})
        if not isinstance(page, list):
            raise SystemExit("bexio answered the contact list with something other than a list")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += len(page)


def list_bills(token):
    """Every purchase bill, paged by page number (the API 4.0 lists)."""
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


def find_vendor(contacts):
    """The one contact whose name holds VENDOR_SEARCH; None when there is none. Two is an error."""
    found = [c for c in contacts if VENDOR_SEARCH in (c.get("name_1") or "").casefold()]
    if len(found) > 1:
        raise SystemExit("{} contacts match '{}' ({}); pick the right one in bexio before running this".format(
            len(found), VENDOR_SEARCH, ", ".join(str(c.get("id")) for c in found)))
    return found[0] if found else None


def contact_body(owner_id, country_id):
    """The JSON that creates the vendor contact. owner_id and user_id are the account's user."""
    return dict(VENDOR_FIELDS, country_id=country_id, user_id=owner_id, owner_id=owner_id)


def bill_body(supplier_id, file_uuid, nr, date, total, text):
    """The JSON of the draft bill: header and total, the uploaded file attached, no positions."""
    return {
        "supplier_id": supplier_id,
        "vendor_ref": nr,
        "bill_date": date,
        "currency_code": "CHF",
        "gross": float(total),
        "title": text,
        "attachment_ids": [file_uuid],
        "status": BILL_STATUS,
    }


def checked_total(text):
    """The total as a Decimal in whole rappen; refuses zero, negative and more than two decimals."""
    try:
        total = Decimal(text)
    except InvalidOperation:
        raise SystemExit("refused total {!r}: not a number".format(text))
    if not total.is_finite() or total <= 0 or total != total.quantize(Decimal("0.01")):
        raise SystemExit("refused total {!r}: a positive amount with at most two decimals".format(text))
    return total.quantize(Decimal("0.01"))


def checked_date(text):
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise SystemExit("refused date {!r}: use YYYY-MM-DD".format(text))


def main(argv):
    parser = argparse.ArgumentParser(description="Create the vendor contact and a draft bill from the uploaded file.")
    parser.add_argument("--file-uuid", required=True, help="the uuid of the file in the bexio inbox (upload_bill.py appends it to private/bexio-upload/last-result.txt)")
    parser.add_argument("--nr", required=True, help="the vendor's invoice number")
    parser.add_argument("--date", required=True, help="the invoice date, YYYY-MM-DD")
    parser.add_argument("--total", required=True, help="the invoice total in CHF, including VAT")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="the booking text (default: %(default)s)")
    parser.add_argument("--yes", action="store_true", help="create for real (default: dry run, nothing is written)")
    args = parser.parse_args(argv)

    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is not set; run through av inject (see finance/bexio/README.md)")
    if not args.file_uuid.strip() or not args.nr.strip() or not args.text.strip():
        raise SystemExit("refused: the file uuid, the invoice number and the booking text must not be empty")
    total = checked_total(args.total)
    date = checked_date(args.date)

    # Reads first: nothing below writes until the whole plan is known and printed.
    if any(str(b.get("vendor_ref") or "") == args.nr for b in list_bills(token)):
        raise SystemExit("refused: a bill with invoice number {} is already in bexio; nothing was created".format(args.nr))
    vendor = find_vendor(list_contacts(token))
    if vendor is not None:
        supplier_id = vendor["id"]
        contact_plan = None
        print("contact: found '{}' as bexio contact id {}; not created".format(vendor.get("name_1"), supplier_id))
    else:
        country = [c for c in _send("GET", "/2.0/country", token) if c.get("iso_3166_alpha2") == VENDOR_COUNTRY]
        if len(country) != 1:
            raise SystemExit("bexio has no single country for {}".format(VENDOR_COUNTRY))
        me = _send("GET", "/3.0/users/me", token)
        contact_plan = contact_body(me["id"], country[0]["id"])
        supplier_id = None
        print("contact: no contact matches '{}'; would create:".format(VENDOR_SEARCH))
        print(json.dumps(contact_plan, ensure_ascii=False, indent=2))

    bill_plan = bill_body(supplier_id, args.file_uuid, args.nr, date, total, args.text)
    print("bill: would create (supplier_id {} once the contact is known):".format(supplier_id if supplier_id else "from the new contact"))
    print(json.dumps(bill_plan, ensure_ascii=False, indent=2))

    if not args.yes:
        print("dry run: nothing created; add --yes to create")
        return

    if contact_plan is not None:
        created = _send("POST", "/2.0/contact", token, body=contact_plan)
        if not isinstance(created, dict) or created.get("id") is None:
            raise SystemExit("bexio answered the contact without an id; check the contacts before running again")
        supplier_id = created["id"]
        line = "contact: created bexio contact id {}".format(supplier_id)
        print(line)
        record_result(line)
    bill_plan["supplier_id"] = supplier_id
    bill = _send("POST", "/4.0/purchase/bills", token, body=bill_plan)
    if not isinstance(bill, dict) or bill.get("id") is None:
        raise SystemExit("bexio answered the bill without an id; check the bills before running again")
    print("bill: created bexio bill id {} as {}, no line items".format(bill["id"], BILL_STATUS))
    record_result("bill: created bexio bill id {} as {}, no line items".format(bill["id"], BILL_STATUS))


if __name__ == "__main__":
    main(sys.argv[1:])
