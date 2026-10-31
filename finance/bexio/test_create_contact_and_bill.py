"""Offline tests for create_contact_and_bill.py: the reviewed requests, the plans, the guards, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import re
import shutil
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(
    os.path.abspath(__file__)
)  # run this checkout's files, never the main checkout's
cb = launcher.load(
    "create_contact_and_bill.py"
)  # the launcher is a bash file; the tests run its python body
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)

TOKEN = "tok-invented-not-real"
FAKE_BILL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BILL_REPLACE_REQUIRED_FIELDS = {
    "document_no",
    "title",
    "supplier_id",
    "amount_calc",
    "manual_amount",
    "contact_partner_id",
    "bill_date",
    "due_date",
    "currency_code",
    "item_net",
    "attachment_ids",
    "address",
    "line_items",
    "discounts",
}
ARGS = [
    "--vendor",
    os.path.join(
        os.environ.get("BEXIO_PRIVATE", "/tmp/bexio-tests"), "vendors", "vendor.json"
    ),
    "--file-uuid",
    "uuid-invented-1",
    "--nr",
    "INV-TEST-1",
    "--date",
    "2026-01-15",
    "--total",
    "733.30",
    "--account",
    "6000",
    "--tax",
    "VM81",
    "--text",
    "Invented supplier service",
]

VENDOR = {
    "name_1": "Muster Reinigung AG",
    "vat_number": "CHE-000.000.001",
    "street_name": "Beispielweg",
    "house_number": "12",
    "postcode": "8000",
    "city": "Beispielstadt",
    "country": "CH",
    "mail": "rechnung@example.invalid",
    "url": "https://example.invalid/vendor",
}
CONTACT = {key: value for key, value in VENDOR.items() if key != "vat_number"}
CONTACT["remarks"] = "UID CHE-000.000.001"


# POST /2.0/contact request fields, copied from https://docs.bexio.com/#tag/Contacts/operation/v2CreateContact.
DOCUMENTED_CONTACT_CREATE_FIELDS = {
    "nr",
    "contact_type_id",
    "name_1",
    "name_2",
    "salutation_id",
    "salutation_form",
    "title_id",
    "birthday",
    "street_name",
    "house_number",
    "address_addition",
    "postcode",
    "city",
    "country_id",
    "mail",
    "mail_second",
    "phone_fixed",
    "phone_fixed_second",
    "phone_mobile",
    "fax",
    "url",
    "skype_name",
    "remarks",
    "language_id",
    "contact_group_ids",
    "contact_branch_ids",
    "user_id",
    "owner_id",
}
CONTACT_EDIT_ACCEPTED_FIELDS = DOCUMENTED_CONTACT_CREATE_FIELDS - {"title_id"}


def _answer(body):
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def fake_bexio(answers):
    """A urlopen stand-in: answers maps (method, path) to a queue of JSON bodies (or exceptions), taken in order.

    Every request is recorded; one the queue does not expect fails the test.
    """
    queues = {key: list(items) for key, items in answers.items()}
    calls = []

    def urlopen(req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        key = (req.get_method(), url.path)
        calls.append((key, req))
        if req.get_method() == "PUT" and url.path.startswith("/4.0/purchase/bills/"):
            body = json.loads(req.data.decode("utf-8"))
            missing = BILL_REPLACE_REQUIRED_FIELDS - set(body)
            if missing:
                raise urllib.error.HTTPError(
                    req.full_url,
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(b'{"error": "missing required field"}'),
                )
        if req.get_method() == "POST" and re.fullmatch(
            r"/2\.0/contact/[0-9]+", url.path
        ):
            body = json.loads(req.data.decode("utf-8"))
            missing = {"contact_type_id", "name_1", "user_id", "owner_id"} - set(body)
            refused = set(body) - CONTACT_EDIT_ACCEPTED_FIELDS
            # Only salutation_id=0 is proven live; rejecting other empty values is a deliberate stricter guess.
            empty_values = any(
                value == "" or (type(value) in (int, float) and value == 0)
                for value in body.values()
            )
            if missing or refused or empty_values:
                raise urllib.error.HTTPError(
                    req.full_url,
                    422,
                    "Unprocessable Entity",
                    {},
                    io.BytesIO(b'{"error": "invalid contact edit fields"}'),
                )
        if not queues.get(key):
            raise AssertionError("unexpected request {} {}".format(*key))
        item = queues[key].pop(0)
        if isinstance(item, Exception):
            raise item
        return _answer(json.dumps(item).encode("utf-8"))

    return urlopen, calls


def _run(argv):
    vendor_path = os.path.join(cb.PRIVATE, "vendors", "vendor.json")
    os.makedirs(os.path.dirname(vendor_path), exist_ok=True)
    if not os.path.exists(vendor_path):
        with open(vendor_path, "w", encoding="utf-8") as f:
            json.dump(VENDOR, f)
    argv = [vendor_path if value == ARGS[1] else value for value in argv]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cb.main(argv)
    return out.getvalue()


def _sent(calls, method, path):
    return [req for (key, req) in calls if key == (method, path)]


def _body(req):
    return json.loads(req.data.decode("utf-8"))


def standard_answers(contacts=None, bills=None):
    """An invented yard with no matching contact or bill, unless the caller says otherwise."""
    return {
        ("GET", "/2.0/contact"): [
            (
                contacts
                if contacts is not None
                else [{"id": 1, "name_1": "Unrelated Vendor"}]
            )
        ],
        ("GET", "/2.0/country"): [
            [{"id": 5, "iso_3166_alpha2": "CH"}, {"id": 6, "iso_3166_alpha2": "DE"}]
        ],
        ("GET", "/3.0/users/me"): [{"id": 9}],
        ("GET", "/4.0/purchase/bills"): [
            {"data": bills if bills is not None else []},
            {
                "data": [
                    b
                    for b in (bills or [])
                    if any(
                        str(c.get("id")) == str(b.get("supplier_id"))
                        for c in (contacts or [])
                    )
                ]
            },
        ],
        ("GET", "/2.0/accounts"): [
            [
                {
                    "id": 16,
                    "account_no": "6000",
                    "name": "Office supplies",
                    "account_type": 2,
                    "is_active": True,
                    "is_locked": False,
                }
            ]
        ],
        ("GET", "/3.0/taxes"): [
            [
                {
                    "id": 23,
                    "code": "VM81",
                    "display_name": "Input VAT 8.1%",
                    "value": 8.1,
                    "type": "pre_tax_material",
                    "is_active": True,
                }
            ]
        ],
        ("POST", "/2.0/contact"): [{"id": 77}],
        ("POST", "/4.0/purchase/bills"): [{"id": FAKE_BILL_ID}],
        ("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID): [
            {
                "id": FAKE_BILL_ID,
                "status": "DRAFT",
                "amount_calc": 733.3,
                "line_items": [{"amount": 733.3, "tax_calc": 0.0}],
                "split_into_line_items": False,
                "contact_partner_id": 9,
            }
        ],
    }


class TokenTest(unittest.TestCase):
    def test_refuses_without_the_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            cb.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaises(SystemExit) as cm:
                _run(ARGS)
        self.assertIn("BEXIO_TOKEN", str(cm.exception))
        urlopen.assert_not_called()


class LauncherTest(unittest.TestCase):
    # The file is blessed by hash, so what it runs with is pinned here: the shebang, the
    # mode, and none of the retired vault proxy's name in the file. That name is put
    # together from parts: the depot gate greps the repo for it and must find none, so
    # this file cannot spell it.
    RETIRED = "".join(("var", "lock"))

    PATH = os.path.join(
        os.path.dirname(os.path.abspath(cb.__file__)), "create_contact_and_bill.py"
    )
    SHEBANG = "#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash"

    def test_the_first_line_is_the_av_inject_shebang_for_bash(self):
        with open(self.PATH) as f:
            first = f.readline().rstrip("\n")
        self.assertEqual(first, self.SHEBANG)

    def test_the_file_is_executable(self):
        self.assertTrue(
            os.access(self.PATH, os.X_OK), "chmod +x create_contact_and_bill.py"
        )

    def test_no_retired_name_is_left_in_the_file(self):
        with open(self.PATH) as f:
            self.assertNotIn(self.RETIRED, f.read().lower())


class ResultFileTest(unittest.TestCase):
    """av inject drops the child's stdout, so a real run also lands in private/bexio-upload/last-result.txt."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        shutil.rmtree(os.path.join(cb.PRIVATE, "bexio-upload"), ignore_errors=True)

    def result(self):
        with open(os.path.join(cb.PRIVATE, "bexio-upload", "last-result.txt")) as f:
            return f.read()

    def test_yes_appends_the_contact_and_bill_ids_and_no_token(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        content = self.result()
        self.assertIn("contact id 77", content)
        self.assertIn("bill id " + FAKE_BILL_ID, content)
        self.assertIn("vendor_ref INV-TEST-1 supplier id 77", content)
        self.assertNotIn(TOKEN, content)
        contact_request = _sent(calls, "POST", "/2.0/contact")[0]
        self.assertEqual(_body(contact_request)["url"], VENDOR["url"])

    def test_an_existing_contact_records_only_the_bill_id(self):
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        del answers[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        content = self.result()
        self.assertNotIn("contact id 77", content)
        self.assertIn("bill id " + FAKE_BILL_ID, content)

    def test_a_dry_run_writes_no_result_file(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(ARGS)
        self.assertFalse(
            os.path.exists(os.path.join(cb.PRIVATE, "bexio-upload", "last-result.txt"))
        )


class PlanDryRunTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_prints_both_plans_and_writes_nothing(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(ARGS)
        self.assertEqual(_sent(calls, "POST", "/2.0/contact"), [])
        self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])
        self.assertIn("would create", out)
        self.assertIn("dry run: nothing created", out)
        self.assertIn('"name_1": "Muster Reinigung AG"', out)
        self.assertIn('"street_name": "Beispielweg"', out)
        self.assertIn('"city": "Beispielstadt"', out)
        self.assertIn('"country_id": 5', out)
        self.assertIn('"owner_id": 9', out)
        self.assertIn('"attachment_ids": [\n    "uuid-invented-1"', out)
        self.assertIn('"amount_calc": 733.3', out)
        self.assertIn('"booking_account_id": 16', out)
        self.assertIn('"tax_id": 23', out)
        self.assertNotIn(TOKEN, out)


class ReverseChargeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with_tax(self, code, tax_type="pre_tax_material", active=True, yes=False):
        answers = standard_answers()
        answers[("GET", "/3.0/taxes")] = [
            [
                {
                    "id": 23,
                    "code": code,
                    "display_name": "Invented tax",
                    "value": 8.1,
                    "type": tax_type,
                    "is_active": active,
                }
            ]
        ]
        argv = list(ARGS)
        argv[argv.index("VM81")] = code
        if yes:
            argv.append("--yes")
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(argv)
        return out, calls

    def test_each_reverse_charge_code_uses_invoice_amount_as_net(self):
        self.assertEqual(cb.REVERSE_CHARGE_CODES, {"BZM77", "BZM81", "BZB77", "BZB81"})
        for code in sorted(cb.REVERSE_CHARGE_CODES):
            with self.subTest(code=code):
                out, calls = self.run_with_tax(code, tax_type="unknown_fixture_type")
                self.assertIn("amount basis: net, reverse charge (Bezugsteuer)", out)
                self.assertIn('"item_net": true', out)
                self.assertIn('"amount_calc": 733.3', out)
                self.assertIn('"manual_amount": false', out)
                bill_json = out.split('"line_items": [', 1)[1]
                self.assertIn('"amount": 733.3', bill_json)
                self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])

    def test_yes_sends_each_reverse_charge_bill_on_net_basis(self):
        for code in sorted(cb.REVERSE_CHARGE_CODES):
            with self.subTest(code=code):
                _, calls = self.run_with_tax(
                    code, tax_type="unknown_fixture_type", yes=True
                )
                bills = _sent(calls, "POST", "/4.0/purchase/bills")
                self.assertEqual(len(bills), 1)
                bill = _body(bills[0])
                self.assertTrue(bill["item_net"])
                self.assertFalse(bill["manual_amount"])
                self.assertEqual(
                    bill["amount_calc"], float(ARGS[ARGS.index("--total") + 1])
                )
                self.assertEqual(bill["line_items"][0]["amount"], bill["amount_calc"])

    def test_reverse_charge_tax_must_be_known_and_unique(self):
        tax = {"code": "BZM81", "type": "unknown_fixture_type", "is_active": True}
        for taxes, error in (([], "unknown tax code"), ([tax, dict(tax)], "ambiguous")):
            with self.subTest(error=error):
                answers = standard_answers()
                answers[("GET", "/3.0/taxes")] = [taxes]
                argv = list(ARGS)
                argv[argv.index("VM81")] = tax["code"]
                urlopen, calls = fake_bexio(answers)
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, error):
                        _run(["--yes"] + argv)
                self.assertFalse(any(key[0] == "POST" for key, _ in calls))

    def test_ordinary_input_vat_stays_gross(self):
        out, _ = self.run_with_tax("VM81")
        self.assertIn("amount basis: gross", out)
        self.assertIn('"item_net": false', out)

    def test_sales_code_still_refuses(self):
        answers = standard_answers()
        answers[("GET", "/3.0/taxes")] = [
            [
                {
                    "id": 24,
                    "code": "SALE81",
                    "type": "sales_tax",
                    "is_active": True,
                }
            ]
        ]
        argv = list(ARGS)
        argv[argv.index("VM81")] = "SALE81"
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "sales VAT code"):
                _run(argv)
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])

    def test_inactive_reverse_charge_code_still_refuses(self):
        answers = standard_answers()
        answers[("GET", "/3.0/taxes")] = [
            [
                {
                    "id": 23,
                    "code": "BZM81",
                    "type": "unknown_fixture_type",
                    "is_active": False,
                }
            ]
        ]
        argv = list(ARGS)
        argv[argv.index("VM81")] = "BZM81"
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "inactive"):
                _run(argv)
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_yes_creates_the_contact_then_the_bill(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        contact = _body(_sent(calls, "POST", "/2.0/contact")[0])
        self.assertEqual(contact["name_1"], "Muster Reinigung AG")
        self.assertEqual(contact["contact_type_id"], 1)
        self.assertEqual(contact["country_id"], 5)
        self.assertNotIn("vat_number", contact)
        self.assertEqual(contact["remarks"], "UID CHE-000.000.001")
        self.assertTrue(set(contact).issubset(cb.CONTACT_CREATE_FIELDS))
        bill = _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])
        self.assertEqual(bill["supplier_id"], 77)
        self.assertEqual(bill["contact_partner_id"], 9)
        self.assertEqual(bill["vendor_ref"], "INV-TEST-1")
        self.assertEqual(bill["bill_date"], "2026-01-15")
        self.assertEqual(bill["currency_code"], "CHF")
        self.assertEqual(bill["amount_calc"], 733.3)
        self.assertNotIn("amount_man", bill)
        self.assertEqual(bill["title"], "Invented supplier service")
        self.assertEqual(bill["attachment_ids"], ["uuid-invented-1"])
        self.assertNotIn("status", bill)
        self.assertEqual(bill["line_items"][0]["amount"], 733.3)
        self.assertEqual(bill["line_items"][0]["booking_account_id"], 16)
        self.assertEqual(bill["line_items"][0]["tax_id"], 23)
        self.assertEqual(bill["currency_code"], "CHF")
        self.assertEqual(bill["due_date"], "2026-02-14")
        self.assertFalse(bill["item_net"])
        self.assertFalse(bill["manual_amount"])
        self.assertEqual(bill["discounts"], [])
        self.assertNotIn("split_into_line_items", bill)
        self.assertNotIn("status", bill)
        self.assertIn("created bexio contact id 77", out)
        self.assertIn("created bexio bill id " + FAKE_BILL_ID, out)
        self.assertNotIn(TOKEN, out)

    def test_an_existing_contact_is_found_and_not_created(self):
        contacts = [{"id": i, "name_1": f"Kunde {i}"} for i in range(cb.PAGE_SIZE)]
        answers = standard_answers()
        answers[("GET", "/2.0/contact")] = [
            contacts,
            [dict(CONTACT, id=12)],
        ]
        del answers[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        self.assertEqual(_sent(calls, "POST", "/2.0/contact"), [])
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 12
        )
        self.assertIn(
            "found 'Muster Reinigung AG' as bexio contact id 12; not created", out
        )

    def test_existing_contact_partner_is_never_used_as_bill_partner(self):
        answers = standard_answers(
            contacts=[dict(CONTACT, id=12, contact_partner_id=888)]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        body = _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])
        self.assertEqual(body["supplier_id"], 12)
        self.assertEqual(body["contact_partner_id"], 9)

    def test_two_matching_contacts_refuse_before_any_write(self):
        answers = standard_answers(
            contacts=[
                dict(CONTACT, id=3),
                dict(CONTACT, id=4),
            ]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("2 contacts match", str(cm.exception))
        self.assertEqual([k for (k, _) in calls if k[0] == "POST"], [])

    def test_an_invoice_number_already_in_bexio_refuses_before_any_write(self):
        urlopen, calls = fake_bexio(
            standard_answers(
                contacts=[dict(CONTACT, id=12)],
                bills=[{"id": 5, "supplier_id": 12, "vendor_ref": "INV-TEST-1"}],
            )
        )
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("already exists for this supplier", str(cm.exception))
        self.assertEqual([k for (k, _) in calls if k[0] == "POST"], [])

    def test_bills_are_read_page_by_page(self):
        full = [{"id": i, "vendor_ref": f"other-{i}"} for i in range(cb.PAGE_SIZE)]
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        answers[("GET", "/4.0/purchase/bills")] = [
            {"data": full},
            {"data": [{"id": 6, "vendor_ref": "INV-TEST-1"}]},
            {"data": [{"id": 6, "vendor_ref": "INV-TEST-1"}]},
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit):
                _run(["--yes"] + ARGS)
        pages = [
            urllib.parse.parse_qs(urllib.parse.urlparse(r.full_url).query)["page"]
            for r in _sent(calls, "GET", "/4.0/purchase/bills")
        ]
        self.assertEqual(pages, [["1"], ["2"], ["1"]])


class PostGuardTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_contact_post_is_not_retried(self):
        answers = standard_answers()
        answers[("POST", "/2.0/contact")] = [
            urllib.error.HTTPError(
                "u", 500, "x", {}, io.BytesIO(b'{"error": "invented"}')
            )
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(len(_sent(calls, "POST", "/2.0/contact")), 1)
        self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_a_failed_bill_post_is_not_retried(self):
        answers = standard_answers()
        answers[("POST", "/4.0/purchase/bills")] = [
            urllib.error.HTTPError(
                "u", 400, "bad", {}, io.BytesIO(b'{"error": "invented"}')
            )
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual(len(_sent(calls, "POST", "/4.0/purchase/bills")), 1)

    def test_only_the_reviewed_requests_are_sent(self):
        refused = [
            ("PUT", "/4.0/purchase/bills"),
            ("POST", "/4.0/purchase/bills/" + FAKE_BILL_ID),
            ("GET", "/4.0/purchase/bills/88"),
            ("PUT", "/4.0/purchase/bills/not-a-uuid"),
            ("DELETE", "/2.0/contact/77"),
            ("POST", "/2.0/contact/search"),
            ("GET", "/2.0/contact/search"),
            ("POST", "/2.0/contact/_bulk_create"),
            ("POST", "/2.0/contact/77/restore"),
            ("POST", "/2.0/contact/77/additional_address"),
            ("GET", "/2.0/contact/77/additional_address"),
            ("POST", "/2.0/contact/77x"),
            ("POST", "/4.0/purchase/bills/88/status"),
            ("GET", "/3.0/files"),
            ("POST", "/3.0/files"),
        ]
        for method, path in refused:
            with self.subTest(method=method, path=path), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as urlopen:
                with self.assertRaises(cb.RefusedRequest):
                    cb._send(method, path, TOKEN, body={})
                urlopen.assert_not_called()


class LinesRepairTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def draft(self, status="DRAFT"):
        return {
            "id": FAKE_BILL_ID,
            "document_no": "DOC-TEST-1",
            "status": status,
            "title": "Invented bill",
            "supplier_id": 12,
            "vendor_ref": "KEEP-REF",
            "contact_partner_id": 12,
            "bill_date": "2026-01-01",
            "due_date": "2026-02-01",
            "currency_code": "CHF",
            "manual_amount": True,
            "amount_man": 733.3,
            "amount_calc": 733.3,
            "item_net": False,
            "split_into_line_items": False,
            "attachment_ids": ["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"],
            "address": {
                "lastname_company": "Invented supplier",
                "type": "COMPANY",
                "ignored": "drop",
            },
            "line_items": [
                {
                    "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "position": 0,
                    "title": "Existing line",
                    "tax_id": 23,
                    "amount": 733.3,
                    "booking_account_id": 16,
                    "tax_calc": None,
                    "ignored": "drop",
                }
            ],
            "discounts": [],
            "ignored": "drop",
        }

    def repair_answers(self, draft=None, readback=None):
        before = draft or self.draft()
        answers = standard_answers()
        answers[("GET", "/4.0/purchase/bills")] = [{"data": [before]}]
        answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [
            before,
            before,
            readback or dict(before, contact_partner_id=9, manual_amount=False),
        ]
        return answers

    def writable_draft(self, before):
        # Compare against the read bill independently of repair_body's normalization.
        expected = json.loads(json.dumps(before))
        for key in ("id", "status", "ignored"):
            expected.pop(key)
        expected["address"].pop("ignored")
        for row in expected["line_items"]:
            row.pop("tax_calc")
            row.pop("ignored", None)
        return expected

    def test_fake_bill_replace_rejects_each_missing_required_field(self):
        path = "/4.0/purchase/bills/" + FAKE_BILL_ID
        complete = cb.repair_body(self.draft(), 9)
        for field in sorted(BILL_REPLACE_REQUIRED_FIELDS):
            with self.subTest(field=field):
                body = dict(complete)
                del body[field]
                urlopen, calls = fake_bexio({("PUT", path): [{}]})
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, "HTTP 400"):
                        cb._send("PUT", path, TOKEN, body=body)
                self.assertEqual(len(_sent(calls, "PUT", path)), 1)

    def test_non_draft_repair_refuses_before_put(self):
        for extra in ([], ["--text", "Renamed"]):
            with self.subTest(extra=extra):
                urlopen, calls = fake_bexio(self.repair_answers(self.draft("BOOKED")))
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, "not DRAFT"):
                        _run(["--repair", "DOC-TEST-1", "--yes"] + extra)
                self.assertEqual(
                    _sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID), []
                )

    def test_repair_dry_run_keeps_values_drops_nulls_and_sends_nothing(self):
        urlopen, calls = fake_bexio(self.repair_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--repair", "DOC-TEST-1"])
        self.assertIn("contact_partner_id: 12 -> 9", out)
        self.assertEqual(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID), [])

    def test_text_rename_single_row_changes_only_titles_and_dry_run_writes_nothing(
        self,
    ):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["tax_calc"] = 0
        answers = self.repair_answers(before)
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--repair", "DOC-TEST-1", "--text", "  New title  "])
        self.assertIn('bill title: "Invented bill" -> "New title"', out)
        self.assertIn('row 1 title: "Existing line" -> "New title"', out)
        self.assertFalse(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID))

    def test_text_rename_multirow_requires_lines(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["amount"] = 366.65
        before["line_items"].append(
            dict(
                before["line_items"][0],
                id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                position=1,
            )
        )
        before["split_into_line_items"] = True
        urlopen, calls = fake_bexio(self.repair_answers(before))
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "--lines is needed"):
                _run(["--repair", "DOC-TEST-1", "--text", "New title"])
        self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_text_rename_lines_preserves_all_existing_ids_and_values(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["tax_calc"] = 0
        before["line_items"][0]["amount"] = 366.65
        before["line_items"].append(
            dict(
                before["line_items"][0],
                id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                position=1,
            )
        )
        before["split_into_line_items"] = True
        path = os.path.join(cb.PRIVATE, "text-rename-lines.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "title": "First invented",
                        "gross": "366.65",
                        "account": "6000",
                        "tax": "VM81",
                    },
                    {
                        "title": "Second invented",
                        "gross": "366.65",
                        "account": "6000",
                        "tax": "VM81",
                    },
                ],
                f,
            )
        before["line_items"].reverse()
        answers = self.repair_answers(before)
        readback = dict(
            before, title="New title", contact_partner_id=9, manual_amount=True
        )
        readback["split_into_line_items"] = True
        readback["line_items"] = [
            dict(row, title=title)
            for row, title in zip(
                sorted(before["line_items"], key=lambda row: row["position"]),
                ["First invented", "Second invented"],
            )
        ]
        answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)][2] = readback
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(
                [
                    "--repair",
                    "DOC-TEST-1",
                    "--text",
                    "New title",
                    "--lines",
                    path,
                    "--yes",
                ]
            )
        put = _body(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)[0])
        self.assertEqual(
            [r["id"] for r in put["line_items"]],
            [
                r["id"]
                for r in sorted(before["line_items"], key=lambda row: row["position"])
            ],
        )
        self.assertEqual([r["position"] for r in put["line_items"]], [0, 1])
        self.assertEqual(
            [r["title"] for r in put["line_items"]],
            ["First invented", "Second invented"],
        )
        for field in ("amount", "booking_account_id", "tax_id"):
            self.assertEqual(
                [r[field] for r in put["line_items"]],
                [
                    r[field]
                    for r in sorted(
                        before["line_items"], key=lambda row: row["position"]
                    )
                ],
            )

    def test_text_validation_happens_before_any_request(self):
        for value, message in [("   ", "must not be empty"), ("x" * 81, "exceeds 80")]:
            with mock.patch.object(cb.urllib.request, "urlopen") as send:
                with self.assertRaisesRegex(SystemExit, message):
                    _run(["--repair", "DOC-TEST-1", "--text", value])
                send.assert_not_called()

    def test_text_lines_refuse_count_and_each_row_value_change(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["tax_calc"] = 0
        path = os.path.join(cb.PRIVATE, "text-rename-guard.json")
        cases = [
            (
                [
                    {"title": "One", "gross": "733.30"},
                    {"title": "Two", "gross": "733.30"},
                ],
                "row count",
                False,
            ),
            ([{"title": "One", "gross": "733.29"}], "amount", False),
            (
                [{"title": "One", "gross": "733.30", "account": "6100"}],
                "booking_account_id",
                True,
            ),
            ([{"title": "One", "gross": "733.30", "tax": "VM82"}], "tax_id", True),
        ]
        for rows, message, add_reference in cases:
            with self.subTest(message=message):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
                answers = self.repair_answers(before)
                if add_reference:
                    answers[("GET", "/2.0/accounts")][0].append(
                        {
                            "id": 17,
                            "account_no": "6100",
                            "account_type": 2,
                            "is_active": True,
                            "is_locked": False,
                        }
                    )
                    answers[("GET", "/3.0/taxes")][0].append(
                        {
                            "id": 24,
                            "code": "VM82",
                            "type": "pre_tax_material",
                            "is_active": True,
                        }
                    )
                urlopen, calls = fake_bexio(answers)
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, message):
                        _run(
                            [
                                "--repair",
                                "DOC-TEST-1",
                                "--text",
                                "New title",
                                "--lines",
                                path,
                                "--account",
                                "6000",
                                "--tax",
                                "VM81",
                                "--yes",
                            ]
                        )
                self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_overlong_title_in_repair_lines_refuses(self):
        path = os.path.join(cb.PRIVATE, "text-rename-long-row.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"title": "x" * 81, "gross": "733.30"}], f)
        prepared = self.draft()
        prepared["contact_partner_id"] = 9
        urlopen, calls = fake_bexio(self.repair_answers(prepared))
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "exceeds 80"):
                _run(
                    [
                        "--repair",
                        "DOC-TEST-1",
                        "--text",
                        "New title",
                        "--lines",
                        path,
                        "--account",
                        "6000",
                        "--tax",
                        "VM81",
                    ]
                )
        self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_title_readback_rejects_each_planned_field_difference(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["tax_calc"] = 0
        expected = [dict(before["line_items"][0], title="New row title")]
        valid = dict(before, title="New title", contact_partner_id=9, amount_calc=733.3)
        valid["line_items"] = [dict(expected[0])]
        mutations = [
            (lambda bill: bill.update(title="Wrong"), "bill title"),
            (lambda bill: bill["line_items"][0].update(title="Wrong"), "row 1 title"),
            (
                lambda bill: bill.update(amount_calc=before["amount_calc"] + 1),
                "amount_calc",
            ),
            (lambda bill: bill.update(contact_partner_id=12), "contact_partner_id"),
            (lambda bill: bill.update(item_net=True), "item_net"),
            (
                lambda bill: bill["line_items"][0].update(booking_account_id=99),
                "booking_account_id",
            ),
            (lambda bill: bill["line_items"][0].update(tax_id=99), "tax_id"),
            (lambda bill: bill["line_items"][0].update(tax_calc=1), "VAT total"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                readback = json.loads(json.dumps(valid))
                mutate(readback)
                with self.assertRaisesRegex(SystemExit, message):
                    cb.check_title_readback(
                        readback, "New title", expected, before, 9, 733.3
                    )

    def test_text_repair_refuses_fractional_amount_difference_without_rounding(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["amount"] = str(
            cb.Decimal(str(before["amount_man"])) + cb.Decimal(1) / 1000
        )
        path = os.path.join(cb.PRIVATE, "fractional-rename.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"title": "Renamed", "gross": str(before["amount_man"])}], f)
        urlopen, calls = fake_bexio(self.repair_answers(before))
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "row 1 amount differs"):
                _run(
                    [
                        "--repair",
                        "DOC-TEST-1",
                        "--text",
                        "Renamed",
                        "--lines",
                        path,
                        "--account",
                        "6000",
                        "--tax",
                        "VM81",
                        "--yes",
                    ]
                )
        self.assertFalse(any(key[0] != "GET" for key, _ in calls))

    def test_text_repair_readback_failures_are_recorded_without_retry(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0]["tax_calc"] = 0
        valid = dict(before, title="Renamed", contact_partner_id=9, manual_amount=True)
        valid["line_items"] = [dict(before["line_items"][0], title="Renamed")]
        mutations = [
            (lambda bill: bill.update(title="Wrong"), "bill title"),
            (lambda bill: bill["line_items"][0].update(title="Wrong"), "row 1 title"),
            (lambda bill: bill.update(amount_calc=None), "amount_calc"),
            (
                lambda bill: bill.update(amount_calc=before["amount_calc"] + 1),
                "amount_calc",
            ),
            (lambda bill: bill.update(contact_partner_id=12), "contact_partner_id"),
            (lambda bill: bill.update(item_net=True), "item_net"),
            (
                lambda bill: bill["line_items"][0].update(booking_account_id=99),
                "booking_account_id",
            ),
            (lambda bill: bill["line_items"][0].update(tax_id=99), "tax_id"),
            (lambda bill: bill["line_items"][0].update(tax_calc=1), "VAT total"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                readback = json.loads(json.dumps(valid))
                mutate(readback)
                answers = self.repair_answers(before, readback)
                answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
                urlopen, calls = fake_bexio(answers)
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ), mock.patch.object(cb, "record_result") as record:
                    with self.assertRaisesRegex(SystemExit, message) as error:
                        _run(["--repair", "DOC-TEST-1", "--text", "Renamed", "--yes"])
                record.assert_any_call(str(error.exception))
                self.assertEqual(
                    [key[0] for key, _ in calls if key[0] != "GET"], ["PUT"]
                )

    def test_text_repair_rejects_unknown_vat_total(self):
        before = self.draft()
        before["contact_partner_id"] = 9
        before["line_items"][0].pop("tax_calc", None)
        readback = dict(
            before, title="Renamed", contact_partner_id=9, manual_amount=True
        )
        readback["line_items"] = [dict(before["line_items"][0], title="Renamed")]
        answers = self.repair_answers(before, readback)
        answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)].append(readback)
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "VAT total"):
                _run(["--repair", "DOC-TEST-1", "--text", "Renamed", "--yes"])
        self.assertEqual([key[0] for key, _ in calls if key[0] != "GET"], ["PUT"])

    def test_text_single_row_write_and_unrepaired_dry_run(self):
        before = self.draft()
        before.update(contact_partner_id=9, manual_amount=False)
        before["line_items"][0]["tax_calc"] = 0
        readback = dict(
            before,
            title="Renamed",
        )
        readback["line_items"] = [dict(before["line_items"][0], title="Renamed")]
        for write in (False, True):
            with self.subTest(write=write):
                answers = self.repair_answers(before, readback)
                answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
                urlopen, calls = fake_bexio(answers)
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    out = _run(
                        ["--repair", "DOC-TEST-1", "--text", "  Renamed  "]
                        + (["--yes"] if write else [])
                    )
                self.assertNotIn("amount_calc:", out)
                self.assertNotIn("contact_partner_id:", out)
                puts = _sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)
                self.assertEqual(len(puts), int(write))
                if write:
                    body = _body(puts[0])
                    expected = self.writable_draft(before)
                    expected["title"] = "Renamed"
                    expected["line_items"][0]["title"] = "Renamed"
                    self.assertEqual(body, expected)

    def test_text_lines_write_sends_full_bill_with_only_titles_changed(self):
        path = os.path.join(cb.PRIVATE, "title-lines-test.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"title": "First renamed row", "gross": "366.65"},
                    {"title": "Second renamed row", "gross": "366.65"},
                ],
                f,
            )
        before = self.draft()
        before.update(
            contact_partner_id=9, manual_amount=False, split_into_line_items=True
        )
        before["line_items"] = [
            dict(before["line_items"][0], amount=366.65),
            {
                "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "position": 1,
                "title": "Existing second row",
                "tax_id": 23,
                "amount": 366.65,
                "booking_account_id": 16,
            },
        ]
        for row in before["line_items"]:
            row["tax_calc"] = 0
        readback = dict(before)
        readback["title"] = "Renamed"
        readback["line_items"] = [
            dict(before["line_items"][0], title="First renamed row"),
            dict(before["line_items"][1], title="Second renamed row"),
        ]
        answers = self.repair_answers(before, readback)
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(
                [
                    "--repair",
                    "DOC-TEST-1",
                    "--text",
                    "Renamed",
                    "--lines",
                    path,
                    "--account",
                    "6000",
                    "--tax",
                    "VM81",
                    "--yes",
                ]
            )
        body = _body(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)[0])
        expected = self.writable_draft(before)
        expected["title"] = "Renamed"
        expected["line_items"][0]["title"] = "First renamed row"
        expected["line_items"][1]["title"] = "Second renamed row"
        self.assertEqual(body, expected)

    def test_text_repair_requires_plain_repair_before_dry_run_or_write(self):
        for field, value in (("amount_calc", None), ("contact_partner_id", 12)):
            for write in (False, True):
                with self.subTest(field=field, write=write):
                    before = self.draft()
                    before[field] = value
                    urlopen, calls = fake_bexio(self.repair_answers(before))
                    with mock.patch.object(
                        cb.urllib.request, "urlopen", side_effect=urlopen
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "run plain --repair first"
                        ):
                            _run(
                                ["--repair", "DOC-TEST-1", "--text", "Renamed"]
                                + (["--yes"] if write else [])
                            )
                    self.assertFalse(any(key[0] != "GET" for key, _ in calls))

    def test_text_repair_keeps_fresh_draft_and_concurrent_edit_refusals(self):
        for field, value, message in [
            ("status", "BOOKED", "no longer DRAFT"),
            ("title", "Concurrent edit", "changed since planning"),
        ]:
            with self.subTest(field=field):
                before = self.draft()
                before["contact_partner_id"] = 9
                answers = self.repair_answers(before)
                answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)][1] = dict(
                    before, **{field: value}
                )
                urlopen, calls = fake_bexio(answers)
                with mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, message):
                        _run(["--repair", "DOC-TEST-1", "--text", "Renamed", "--yes"])
                self.assertFalse(any(key[0] != "GET" for key, _ in calls))

    def test_repair_lines_preserve_total_and_line_ids(self):
        path = os.path.join(cb.PRIVATE, "lines-test.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"title": "First invented service", "gross": "366.65"},
                    {"title": "Second invented service", "gross": "366.65"},
                ],
                f,
            )
        before = self.draft()
        answers = self.repair_answers(before)
        # Fresh status read and final read-back use independent API responses.
        answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [
            before,
            before,
            dict(
                before,
                contact_partner_id=9,
                manual_amount=False,
                split_into_line_items=True,
                line_items=[
                    {
                        "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                        "amount": 366.65,
                        "tax_calc": 0.0,
                    },
                    {"id": None, "amount": 366.65, "tax_calc": 0.0},
                ],
            ),
        ]
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [
            dict(before, manual_amount=False)
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(
                [
                    "--repair",
                    "DOC-TEST-1",
                    "--lines",
                    path,
                    "--account",
                    "6000",
                    "--tax",
                    "VM81",
                    "--yes",
                ]
            )
        put = _body(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)[0])
        self.assertEqual(
            [line["id"] for line in put["line_items"]],
            [before["line_items"][0]["id"], None],
        )
        self.assertEqual(put["amount_calc"], 733.3)
        self.assertTrue(put["split_into_line_items"])
        self.assertEqual(put["document_no"], before["document_no"])
        self.assertEqual(put["supplier_id"], before["supplier_id"])
        self.assertNotIn("ignored", put)
        self.assertNotIn("tax_calc", put["line_items"][0])

    def test_repair_lines_with_wrong_total_refuses(self):
        path = os.path.join(cb.PRIVATE, "lines-wrong-total.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"title": "Invented service", "gross": "366.64"}], f)
        urlopen, calls = fake_bexio(self.repair_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "current total"):
                _run(
                    [
                        "--repair",
                        "DOC-TEST-1",
                        "--lines",
                        path,
                        "--account",
                        "6000",
                        "--tax",
                        "VM81",
                        "--yes",
                    ]
                )
        self.assertEqual(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID), [])

    def test_repair_readback_rejects_changed_total(self):
        readback = dict(
            self.draft(), contact_partner_id=9, manual_amount=False, amount_calc=700.0
        )
        answers = self.repair_answers(readback=readback)
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "amount_calc changed"):
                _run(["--repair", "DOC-TEST-1", "--yes"])
        self.assertEqual(
            len(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)), 1
        )

    def test_readback_rejects_null_amount_calc(self):
        answers = standard_answers()
        answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [
            {"amount_calc": None, "line_items": [], "split_into_line_items": False}
        ]
        urlopen, _ = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "amount_calc is null"):
                cb.report_bill("create", {"amount_calc": None}, 1)

    def test_create_lines_are_gross_resolved_and_must_sum_to_total(self):
        path = os.path.join(cb.PRIVATE, "create-lines-test.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"title": "Invented first service", "gross": "366.65"},
                    {"title": "Invented second service", "gross": "366.65"},
                ],
                f,
            )
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--lines", path] + ARGS)
        self.assertIn('"split_into_line_items": true', out)
        self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        rows[1]["gross"] = "366.64"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "sum to --total"):
                _run(["--lines", path] + ARGS)
        self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])


class RepairReviewTest(unittest.TestCase):
    setUp = LinesRepairTest.setUp
    draft = LinesRepairTest.draft
    repair_answers = LinesRepairTest.repair_answers

    def line_file(self, rows):
        path = os.path.join(cb.PRIVATE, "review-lines.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        return path

    def run_fake(self, argv, answers):
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(argv)
        return out, calls

    def test_no_replacement_refuses_to_change_manual_total(self):
        before = self.draft()
        before["line_items"][0]["amount"] = 366.65
        urlopen, calls = fake_bexio(self.repair_answers(before))
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "current total"):
                _run(["--repair", "DOC-TEST-1", "--yes"])
        self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_missing_current_total_refuses(self):
        for manual in [True, False]:
            before = dict(
                self.draft(), manual_amount=manual, amount_man=None, amount_calc=None
            )
            with self.subTest(manual=manual):
                with self.assertRaisesRegex(
                    SystemExit, "current total is missing or invalid"
                ):
                    cb.repair_body(before, 9)

    def test_fresh_get_refuses_status_or_content_changes(self):
        before = self.draft()
        for changed in [
            dict(before, status="BOOKED"),
            dict(before, vendor_ref="EDITED-REF"),
            dict(before, amount_man=366.65),
        ]:
            answers = self.repair_answers(before)
            answers[("GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)][1] = changed
            urlopen, calls = fake_bexio(answers)
            with self.subTest(changed=changed), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ):
                with self.assertRaisesRegex(SystemExit, "status changed|bill changed"):
                    _run(["--repair", "DOC-TEST-1", "--yes"])
            self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_repair_preserves_fields_ids_and_drops_nulls_at_each_level(self):
        before = self.draft()
        before.update(
            amount_calc=None,
            exchange_rate=None,
            base_currency_amount=None,
            payment={
                "type": "MANUAL",
                "execution_date": "2026-02-01",
                "amount": 733.3,
                "salary_payment": False,
                "note": None,
                "ignored": "drop",
            },
        )
        before["address"].update(title=None, city="Invented city")
        before["line_items"][0]["title"] = None
        before["discounts"] = [
            {
                "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "position": 0,
                "amount": 0.0,
                "ignored": None,
            }
        ]
        answers = self.repair_answers(
            before, dict(before, amount_calc=733.3, manual_amount=False)
        )
        answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
        out, calls = self.run_fake(["--repair", "DOC-TEST-1", "--yes"], answers)
        put = _body(_sent(calls, "PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)[0])
        for key in [
            "document_no",
            "supplier_id",
            "vendor_ref",
            "title",
            "bill_date",
            "due_date",
            "currency_code",
            "attachment_ids",
            "item_net",
            "split_into_line_items",
        ]:
            self.assertEqual(put[key], before[key])
        self.assertEqual(put["line_items"][0]["id"], before["line_items"][0]["id"])
        self.assertEqual(put["discounts"][0]["id"], before["discounts"][0]["id"])
        self.assertNotIn("exchange_rate", put)
        self.assertNotIn("title", put["address"])
        self.assertNotIn("title", put["line_items"][0])
        self.assertNotIn("note", put["payment"])
        self.assertNotIn("ignored", put["payment"])
        self.assertNotIn("ignored", put["discounts"][0])
        self.assertEqual(put["address"]["city"], before["address"]["city"])
        self.assertIn("VAT total:", out)
        with open(os.path.join(cb.PRIVATE, cb.RESULT_FOLDER, cb.RESULT_FILE)) as f:
            self.assertIn("VAT total:", f.read())

    def test_repair_refuses_create_arguments_including_explicit_default_currency(self):
        for flag, value in [
            ("--currency", "CHF"),
            ("--total", "733.30"),
            ("--nr", "TEST"),
            ("--vendor-name", "Invented"),
            ("--date", "2026-01-01"),
            ("--file-uuid", FAKE_BILL_ID),
            ("--account", "6000"),
            ("--tax", "VM81"),
        ]:
            with self.subTest(flag=flag), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as send:
                with self.assertRaises(SystemExit):
                    _run(["--repair", "DOC-TEST-1", flag, value])
                send.assert_not_called()

    def test_repair_requires_unique_document_number_and_uuid(self):
        for rows in [
            [],
            [self.draft(), self.draft()],
            [dict(self.draft(), id="-" * 36)],
        ]:
            answers = self.repair_answers()
            answers[("GET", "/4.0/purchase/bills")] = [{"data": rows}]
            urlopen, calls = fake_bexio(answers)
            with self.subTest(count=len(rows)), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ):
                with self.assertRaisesRegex(SystemExit, "exactly one|not a UUID"):
                    _run(["--repair", "DOC-TEST-1", "--yes"])
            self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_line_file_shape_and_amount_guards(self):
        good = {"title": "Invented service", "gross": "733.30"}
        for rows in [
            {},
            [],
            [good] * 101,
            [dict(good, extra=True)],
            [dict(good, title=" ")],
            [dict(good, gross="0")],
            [dict(good, gross="-1")],
            [dict(good, gross="NaN")],
            [dict(good, gross="invalid")],
            [dict(good, gross="0.001")],
        ]:
            path = self.line_file(rows)
            urlopen, calls = fake_bexio(standard_answers())
            with self.subTest(
                rows=rows[:1] if isinstance(rows, list) else rows
            ), mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
                with self.assertRaises(SystemExit):
                    _run(["--yes", "--lines", path] + ARGS)
            self.assertFalse(any(key[0] == "POST" for key, _ in calls))

    def test_lines_missing_account_or_tax_has_actionable_message(self):
        cases = [
            (
                [{"title": "Invented service", "gross": "733.30", "tax": "VM81"}],
                "account is missing",
            ),
            (
                [{"title": "Invented service", "gross": "733.30", "account": "6000"}],
                "tax is missing",
            ),
        ]
        for rows, message in cases:
            path = self.line_file(rows)
            with self.subTest(message=message), self.assertRaisesRegex(
                SystemExit, message
            ):
                cb.load_lines(
                    path,
                    "",
                    "",
                    standard_answers()[("GET", "/2.0/accounts")][0],
                    standard_answers()[("GET", "/3.0/taxes")][0],
                )

    def test_line_path_cannot_escape_private_through_symlink(self):
        path = os.path.join(cb.PRIVATE, "review-outside-link.json")
        os.symlink(__file__, path)
        with self.assertRaisesRegex(SystemExit, "under the private"):
            cb.load_lines(path, "", "", [], [])

    def test_overridden_accounts_and_taxes_are_resolved_before_writes(self):
        for row in [
            {"title": "Invented", "gross": "733.30", "account": "missing"},
            {"title": "Invented", "gross": "733.30", "tax": "missing"},
        ]:
            path = self.line_file([row])
            urlopen, calls = fake_bexio(standard_answers())
            with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
                with self.assertRaisesRegex(SystemExit, "unknown account|unknown tax"):
                    _run(["--yes", "--lines", path] + ARGS)
            self.assertFalse(any(key[0] == "POST" for key, _ in calls))

    def test_actual_line_tax_controls_create_and_repair_basis(self):
        for repair in [False, True]:
            for code in ["VM81", "BZB81"]:
                path = self.line_file(
                    [
                        {
                            "title": "Invented",
                            "gross": "733.30",
                            "account": "6000",
                            "tax": code,
                        }
                    ]
                )
                answers = self.repair_answers() if repair else standard_answers()
                answers[("GET", "/3.0/taxes")][0].append(
                    {"id": 24, "code": "BZB81", "is_active": True}
                )
                if repair:
                    args = ["--repair", "DOC-TEST-1", "--lines", path, "--yes"]
                    answers[("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)] = [{}]
                else:
                    args = ["--yes", "--lines", path] + ARGS
                out, calls = self.run_fake(args, answers)
                method, route = (
                    ("PUT", "/4.0/purchase/bills/" + FAKE_BILL_ID)
                    if repair
                    else ("POST", "/4.0/purchase/bills")
                )
                self.assertEqual(
                    _body(_sent(calls, method, route)[0])["item_net"], code == "BZB81"
                )

    def test_mixed_line_bases_refuse_before_contact_creation(self):
        path = self.line_file(
            [
                {"title": "Invented gross", "gross": "366.65", "tax": "VM81"},
                {"title": "Invented net", "gross": "366.65", "tax": "BZB81"},
            ]
        )
        answers = standard_answers()
        answers[("GET", "/3.0/taxes")][0].append(
            {"id": 24, "code": "BZB81", "is_active": True}
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "cannot share a bill"):
                _run(["--yes", "--lines", path] + ARGS)
        self.assertFalse(any(key[0] == "POST" for key, _ in calls))

    def test_users_me_missing_id_refuses_before_any_write(self):
        for repair in [False, True]:
            answers = self.repair_answers() if repair else standard_answers()
            answers[("GET", "/3.0/users/me")] = [{}]
            urlopen, calls = fake_bexio(answers)
            with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
                with self.assertRaisesRegex(SystemExit, "users/me without an id"):
                    _run(
                        ["--repair", "DOC-TEST-1", "--yes"]
                        if repair
                        else ["--yes"] + ARGS
                    )
            self.assertFalse(any(key[0] in {"POST", "PUT"} for key, _ in calls))

    def test_readback_failures_send_one_write_and_never_retry(self):
        for repair in [False, True]:
            cases = [
                (None, "bill object"),
                ({"amount_calc": None}, "amount_calc is null"),
                (
                    {
                        "amount_calc": 733.3,
                        "line_items": [{}, {}],
                        "split_into_line_items": False,
                    },
                    "split_into_line_items",
                ),
                (
                    {
                        "amount_calc": 733.3,
                        "line_items": [{}],
                        "split_into_line_items": True,
                    },
                    "expected 2 line items, got 1",
                ),
            ]
            if repair:
                cases.append(
                    (
                        {
                            "amount_calc": 700.0,
                            "line_items": [{}, {}],
                            "split_into_line_items": True,
                        },
                        "amount_calc changed from 733.3 to 700.0",
                    )
                )
            for response, message in cases:
                path = self.line_file(
                    [
                        {"title": "Invented A", "gross": "366.65"},
                        {"title": "Invented B", "gross": "366.65"},
                    ]
                )
                answers = self.repair_answers() if repair else standard_answers()
                route = "/4.0/purchase/bills/" + FAKE_BILL_ID
                if repair:
                    answers[("GET", route)][2] = response
                    answers[("PUT", route)] = [{}]
                    args = [
                        "--repair",
                        "DOC-TEST-1",
                        "--yes",
                        "--lines",
                        path,
                        "--account",
                        "6000",
                        "--tax",
                        "VM81",
                    ]
                else:
                    answers[("GET", route)] = [response]
                    args = ["--yes", "--lines", path] + ARGS
                urlopen, calls = fake_bexio(answers)
                with self.subTest(repair=repair, response=response), mock.patch.object(
                    cb.urllib.request, "urlopen", side_effect=urlopen
                ):
                    with self.assertRaisesRegex(SystemExit, message):
                        _run(args)
                writes = [
                    key
                    for key, _ in calls
                    if key[0] == ("PUT" if repair else "POST")
                    and key[1].startswith("/4.0")
                ]
                self.assertEqual(len(writes), 1)
                if not repair:
                    self.assertFalse(any(key[0] == "PUT" for key, _ in calls))

    def test_bookings_paths_are_never_reviewed(self):
        for method in ["GET", "PUT", "POST"]:
            with mock.patch.object(cb.urllib.request, "urlopen") as send:
                with self.assertRaises(cb.RefusedRequest):
                    cb._send(
                        method,
                        "/4.0/purchase/bills/" + FAKE_BILL_ID + "/bookings/BOOKED",
                        TOKEN,
                    )
                send.assert_not_called()

    def test_create_readback_summary_is_recorded(self):
        with mock.patch.object(cb, "record_result") as record:
            out, _ = self.run_fake(["--yes"] + ARGS, standard_answers())
        summary = record.call_args_list[-1].args[0]
        for field in [
            "line_count=1",
            "split_into_line_items=False",
            "contact_partner_id=9",
            "amount_calc_set=True",
            "VAT total=0.0",
        ]:
            self.assertIn(field, summary)
        self.assertIn(summary, out)


class ArgumentTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bad_totals_and_dates_send_nothing(self):
        bad_totals = ["0", "-5", "2.001", "abc", "nan"]
        bad_dates = ["2026-13-01", "15.01.2026"]
        base = [
            "--yes",
            "--vendor",
            ARGS[1],
            "--file-uuid",
            "uuid-invented-1",
            "--nr",
            "INV-TEST-1",
            "--account",
            "6000",
            "--tax",
            "VM81",
            "--text",
            "Invented service",
        ]
        cases = [base + ["--date", "2026-01-15", "--total", t] for t in bad_totals]
        cases += [base + ["--date", d, "--total", "733.30"] for d in bad_dates]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as urlopen:
                with self.assertRaises(SystemExit):
                    _run(argv)
                urlopen.assert_not_called()

    def test_empty_invoice_number_sends_nothing(self):
        with mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(
                    [
                        "--yes",
                        "--vendor",
                        ARGS[1],
                        "--file-uuid",
                        "uuid-invented-1",
                        "--nr",
                        " ",
                        "--date",
                        "2026-01-15",
                        "--total",
                        "733.30",
                        "--account",
                        "6000",
                        "--tax",
                        "VM81",
                        "--text",
                        "Invented service",
                    ]
                )
        urlopen.assert_not_called()


class ContactBodyTest(unittest.TestCase):
    def test_allowlist_matches_documented_create_schema(self):
        self.assertEqual(cb.CONTACT_CREATE_FIELDS, DOCUMENTED_CONTACT_CREATE_FIELDS)


class FillContactTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def contact(self):
        return {
            "id": 42,
            "updated_at": "old",
            "contact_type_id": 1,
            "nr": "C-42",
            "name_1": VENDOR["name_1"],
            "user_id": 9,
            "owner_id": 9,
            "mail": None,
            "url": "",
            "remarks": None,
            "contact_group_ids": "3",
            "contact_branch_ids": None,
            "is_lead": False,
            "address": {"street": "invented"},
            "unknown_field": "keep",
        }

    def vendor_file(self, vendor=None):
        path = os.path.join(cb.PRIVATE, "vendors", "fill-vendor.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(vendor or VENDOR, f)
        return path

    def run_fill(self, answers, extra=(), vendor=None):
        path = self.vendor_file(vendor)
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--fill-contact", "42", "--vendor", path] + list(extra))
        return out, calls

    def test_dry_run_and_yes_fill_empty_fields_without_losing_read_fields(self):
        before = self.contact()
        filled = dict(
            before,
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
            updated_at="new",
        )
        answers = {("GET", "/2.0/contact/42"): [before]}
        out, calls = self.run_fill(answers)
        self.assertIn("dry run: nothing updated; add --yes to fill", out)
        self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before), filled],
            ("POST", "/2.0/contact/42"): [{}],
        }
        out, calls = self.run_fill(answers, ["--yes"])
        body = _body(_sent(calls, "POST", "/2.0/contact/42")[0])
        self.assertEqual(body["contact_group_ids"], "3")
        self.assertNotIn("contact_branch_ids", body)
        self.assertNotIn("title_id", body)
        self.assertNotIn("is_lead", body)
        self.assertNotIn("id", body)
        self.assertNotIn("updated_at", body)
        self.assertNotIn("address", body)
        self.assertNotIn("unknown_field", body)
        self.assertIn("contact fill: id 42, keys mail,remarks,url, read-back yes", out)
        with open(os.path.join(cb.PRIVATE, "bexio-upload", "last-result.txt")) as f:
            result = f.read()
        self.assertIn("keys mail,remarks,url, read-back yes", result)
        self.assertNotIn(VENDOR["mail"], result)

    def test_empty_variants_filled_fields_and_text_remarks_are_kept(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                before = self.contact()
                before.update(
                    mail=value, url="https://already.invalid", remarks="operator note"
                )
                answers = {("GET", "/2.0/contact/42"): [before]}
                out, calls = self.run_fill(answers)
                self.assertIn("fill: mail", out)
                self.assertIn("kept: url, differs from the file", out)
                self.assertIn("kept: remarks, UID not written", out)
                self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

    def test_populated_mail_is_kept_in_the_full_edit_body(self):
        before = dict(self.contact(), mail="existing@example.invalid")
        after = dict(
            before,
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
            updated_at="changed",
        )
        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before), after],
            ("POST", "/2.0/contact/42"): [{}],
        }
        out, calls = self.run_fill(answers, ["--yes"])
        body = _body(_sent(calls, "POST", "/2.0/contact/42")[0])
        self.assertEqual(body["mail"], "existing@example.invalid")
        self.assertIn("kept: mail, differs from the file", out)
        self.assertEqual(body["url"], VENDOR["url"])

    def test_null_key_removed_on_readback_is_a_difference(self):
        before = self.contact()
        after = dict(
            before,
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
        )
        del after["contact_branch_ids"]
        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before), after],
            ("POST", "/2.0/contact/42"): [{}],
        }
        path = self.vendor_file()
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "contact_branch_ids changed"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        self.assertEqual(len(_sent(calls, "POST", "/2.0/contact/42")), 1)

    def test_mismatch_uid_name_nothing_to_fill_and_stale_read_refuse(self):
        for vendor, contact in [
            (VENDOR, dict(self.contact(), name_1="Other Vendor")),
            (
                dict(VENDOR, vat_number="CHE-000.000.002"),
                dict(self.contact(), remarks="UID CHE-000.000.001"),
            ),
        ]:
            answers = {("GET", "/2.0/contact/42"): [contact]}
            path = self.vendor_file(vendor)
            urlopen, calls = fake_bexio(answers)
            with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
                with self.assertRaises(SystemExit):
                    _run(["--fill-contact", "42", "--vendor", path])
            self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

        no_fill = dict(
            self.contact(),
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
        )
        out, calls = self.run_fill({("GET", "/2.0/contact/42"): [no_fill]})
        self.assertIn("nothing to fill", out)
        self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

        before = self.contact()
        changed = dict(before, mail="newer@example.invalid")
        answers = {("GET", "/2.0/contact/42"): [before, changed]}
        path = self.vendor_file()
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "changed since planning"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

    def test_uid_match_allows_different_name_and_bad_arguments_are_refused(self):
        before = dict(
            self.contact(),
            name_1="Other Vendor",
            remarks="UID CHE-000.000.001",
            mail=VENDOR["mail"],
            url=VENDOR["url"],
        )
        out, calls = self.run_fill({("GET", "/2.0/contact/42"): [before]})
        self.assertIn("nothing to fill", out)
        for argv in (
            ["--fill-contact", "4x", "--vendor", self.vendor_file()],
            ["--fill-contact", "42", "--vendor", self.vendor_file(), "--repair", "DOC"],
            ["--fill-contact", "42", "--vendor", self.vendor_file(), "--repair", ""],
            ["--fill-contact", "42", "--vendor", self.vendor_file(), "--nr", "X"],
        ):
            with self.subTest(argv=argv), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as urlopen:
                with self.assertRaises(SystemExit):
                    _run(argv)
                urlopen.assert_not_called()

    def test_invalid_mail_and_url_are_refused_by_vendor_loader(self):
        for key, value in (
            ("mail", "bad mail@example.invalid"),
            ("mail", "a@@example.invalid"),
            ("url", "ftp://example.invalid"),
            ("url", "https://bad url.invalid"),
        ):
            path = self.vendor_file(dict(VENDOR, **{key: value}))
            with self.subTest(key=key, value=value), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as urlopen:
                with self.assertRaisesRegex(SystemExit, f"refused vendor file: {key}"):
                    _run(["--fill-contact", "42", "--vendor", path])
                urlopen.assert_not_called()

    def test_readback_detects_any_change_and_never_retries(self):
        before = self.contact()
        planned = dict(
            before,
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
        )
        for key, value in (
            ("mail", "wrong@example.invalid"),
            ("url", "https://wrong.invalid"),
            ("remarks", "changed"),
            ("city", "changed"),
            ("address", {"street": "changed"}),
        ):
            readback = dict(planned, **{key: value})
            answers = {
                ("GET", "/2.0/contact/42"): [before, dict(before), readback],
                ("POST", "/2.0/contact/42"): [{}],
            }
            path = self.vendor_file()
            urlopen, calls = fake_bexio(answers)
            with self.subTest(key=key), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ):
                with self.assertRaisesRegex(
                    SystemExit, f"read-back failed: {key} changed"
                ):
                    _run(["--fill-contact", "42", "--vendor", path, "--yes"])
            self.assertEqual(len(_sent(calls, "POST", "/2.0/contact/42")), 1)

    def test_failed_read_after_write_records_failure_without_retry_or_values(self):
        before = self.contact()
        for failure in (
            urllib.error.URLError("invented failure"),
            urllib.error.HTTPError(
                "https://example.invalid",
                503,
                "unavailable",
                {},
                io.BytesIO(b'{"message":"private detail"}'),
            ),
            json.JSONDecodeError("invalid JSON", "", 0),
            None,
        ):
            answers = {
                ("GET", "/2.0/contact/42"): [before, dict(before), failure],
                ("POST", "/2.0/contact/42"): [{}],
            }
            path = self.vendor_file()
            urlopen, calls = fake_bexio(answers)
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ), mock.patch.object(cb, "record_result") as record:
                with self.assertRaises(SystemExit):
                    _run(["--fill-contact", "42", "--vendor", path, "--yes"])
                record.assert_any_call(
                    "contact fill: id 42, keys mail,remarks,url, read-back no"
                )
                self.assertNotIn("private detail", str(record.call_args_list))
            self.assertEqual(len(_sent(calls, "POST", "/2.0/contact/42")), 1)

    def test_each_empty_variant_is_filled_and_all_edit_fields_are_preserved(self):
        self.assertEqual(cb.CONTACT_EDIT_FIELDS, CONTACT_EDIT_ACCEPTED_FIELDS)
        for empty in (None, "", " \t\n"):
            before = dict.fromkeys(CONTACT_EDIT_ACCEPTED_FIELDS, None)
            before.update(self.contact(), mail=empty, url=empty, remarks=empty)
            before["contact_branch_ids"] = "4,5"
            body, fills, kept, omitted = cb.fill_contact_plan(before, VENDOR)
            self.assertEqual(
                fills,
                {
                    "mail": VENDOR["mail"],
                    "url": VENDOR["url"],
                    "remarks": "UID CHE-000.000.001",
                },
            )
            self.assertEqual(
                set(body), (set(cb.CONTACT_EDIT_FIELDS) - omitted) | set(fills)
            )
            self.assertFalse(
                any(
                    value is None or value == "" or value == 0
                    for value in body.values()
                )
            )
            self.assertEqual(kept, [])

    def test_company_and_person_salutations_fill_in_dry_run_and_yes(self):
        shapes = (
            {"salutation_id": 0, "salutation_form": None},
            {"salutation_id": 2, "salutation_form": 0},
        )
        for shape in shapes:
            for yes in (False, True):
                before = dict(self.contact(), **shape)
                after = dict(
                    before,
                    mail=VENDOR["mail"],
                    url=VENDOR["url"],
                    remarks="UID CHE-000.000.001",
                )
                answers = {
                    ("GET", "/2.0/contact/42"): (
                        [before, dict(before), after] if yes else [before]
                    )
                }
                if yes:
                    answers[("POST", "/2.0/contact/42")] = [{}]
                out, calls = self.run_fill(answers, ["--yes"] if yes else [])
                sent = _sent(calls, "POST", "/2.0/contact/42")
                self.assertEqual(len(sent), 1 if yes else 0)
                if yes:
                    body = _body(sent[0])
                    self.assertFalse(
                        any(
                            value is None or value == "" or value == 0
                            for value in body.values()
                        )
                    )
                    if shape["salutation_id"]:
                        self.assertEqual(body["salutation_id"], shape["salutation_id"])
                self.assertIn("fill:", out)

    def test_readback_allows_omitted_empty_to_change_form_but_not_gain_value(self):
        before = dict(self.contact(), salutation_id=0, salutation_form=None)
        after = dict(
            before,
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
            salutation_id=None,
        )
        out, calls = self.run_fill(
            {
                ("GET", "/2.0/contact/42"): [before, dict(before), after],
                ("POST", "/2.0/contact/42"): [{}],
            },
            ["--yes"],
        )
        self.assertIn("read-back yes", out)
        gained = dict(after, salutation_form=1)
        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before), gained],
            ("POST", "/2.0/contact/42"): [{}],
        }
        path = self.vendor_file()
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "salutation_form changed"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        self.assertEqual(len(_sent(calls, "POST", "/2.0/contact/42")), 1)

    def test_fake_probe_rejects_old_full_body_once_without_result(self):
        before = dict(self.contact(), salutation_id=0, salutation_form=None)
        answers = {("GET", "/2.0/contact/42"): [before, dict(before)]}
        path = self.vendor_file()
        urlopen, calls = fake_bexio(answers)
        plan = cb.fill_contact_plan

        def old_full_body(contact, vendor):
            body, fills, kept, omitted = plan(contact, vendor)
            for key in omitted:
                body[key] = contact[key]
            return body, fills, kept, omitted

        with mock.patch.object(
            cb, "fill_contact_plan", side_effect=old_full_body
        ), mock.patch.object(
            cb.urllib.request, "urlopen", side_effect=urlopen
        ), mock.patch.object(
            cb, "record_result"
        ) as record:
            with self.assertRaisesRegex(SystemExit, "HTTP 422"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        self.assertEqual(len(_sent(calls, "POST", "/2.0/contact/42")), 1)
        record.assert_not_called()

    def test_title_and_lead_flags_refuse_dry_run_and_yes_without_post(self):
        for key, value in (("title_id", 1), ("title_id", 0), ("is_lead", True)):
            for yes in (False, True):
                before = self.contact()
                before[key] = value
                answers = {("GET", "/2.0/contact/42"): [before]}
                with self.subTest(key=key, yes=yes):
                    path = self.vendor_file()
                    urlopen, calls = fake_bexio(answers)
                    with mock.patch.object(
                        cb.urllib.request, "urlopen", side_effect=urlopen
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, f"contact has {key} set"
                        ) as error:
                            _run(
                                ["--fill-contact", "42", "--vendor", path]
                                + (["--yes"] if yes else [])
                            )
                    self.assertNotIn(str(value), str(error.exception))
                    self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])

    def test_null_title_and_false_lead_are_filled(self):
        before = dict(self.contact(), title_id=None, is_lead=False)
        after = dict(
            before,
            mail=VENDOR["mail"],
            url=VENDOR["url"],
            remarks="UID CHE-000.000.001",
        )
        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before), after],
            ("POST", "/2.0/contact/42"): [{}],
        }
        out, calls = self.run_fill(answers, ["--yes"])
        body = _body(_sent(calls, "POST", "/2.0/contact/42")[0])
        self.assertNotIn("title_id", body)
        self.assertNotIn("is_lead", body)
        self.assertIn("read-back yes", out)

    def test_fake_rejects_title_id_body_without_retry_or_success_record(self):
        before = self.contact()
        answers = {
            ("GET", "/2.0/contact/42"): [before, dict(before)],
        }
        path = self.vendor_file()
        urlopen, calls = fake_bexio(answers)
        plan = cb.fill_contact_plan

        def plan_with_refused_field(contact, vendor):
            body, fills, kept, omitted = plan(contact, vendor)
            body["title_id"] = 1
            return body, fills, kept, omitted

        with mock.patch.object(
            cb, "fill_contact_plan", side_effect=plan_with_refused_field
        ), mock.patch.object(
            cb.urllib.request, "urlopen", side_effect=urlopen
        ), mock.patch.object(
            cb, "record_result"
        ) as record:
            with self.assertRaisesRegex(SystemExit, "HTTP 422"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        sent = _sent(calls, "POST", "/2.0/contact/42")
        self.assertEqual(len(sent), 1)
        self.assertIn("title_id", _body(sent[0]))
        record.assert_not_called()

    def test_fake_rejects_each_unsupported_or_missing_required_edit_field(self):
        body, _, _, _ = cb.fill_contact_plan(self.contact(), VENDOR)
        invalid = [dict(body, **{key: None}) for key in ("title_id", "is_lead")]
        for key in ("contact_type_id", "name_1", "user_id", "owner_id"):
            invalid.append(
                {field: value for field, value in body.items() if field != key}
            )
        for candidate in invalid:
            with self.subTest(fields=sorted(candidate)):
                urlopen, calls = fake_bexio({("POST", "/2.0/contact/42"): [{}]})
                request = cb.urllib.request.Request(
                    "https://example.invalid/2.0/contact/42",
                    data=json.dumps(candidate).encode("utf-8"),
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 422)
                self.assertEqual(len(calls), 1)

    def test_populated_website_and_text_remarks_survive_the_sent_body(self):
        before = dict(
            self.contact(), url="https://existing.invalid", remarks="invented note"
        )
        after = dict(before, mail=VENDOR["mail"])
        out, calls = self.run_fill(
            {
                ("GET", "/2.0/contact/42"): [before, dict(before), after],
                ("POST", "/2.0/contact/42"): [{}],
            },
            ["--yes"],
        )
        body = _body(_sent(calls, "POST", "/2.0/contact/42")[0])
        for key in ("url", "remarks"):
            self.assertEqual(body[key], before[key])
            self.assertNotIn(before[key], out)

    def test_missing_required_fields_and_every_create_option_refuse(self):
        for key in ("contact_type_id", "name_1", "user_id", "owner_id", "nr"):
            for value in (None, "", 0):
                for yes in (False, True):
                    before = dict(self.contact(), nr="C-42")
                    before[key] = value
                    answers = {("GET", "/2.0/contact/42"): [before]}
                    path = self.vendor_file()
                    urlopen, calls = fake_bexio(answers)
                    with self.subTest(key=key, value=value, yes=yes), mock.patch.object(
                        cb.urllib.request, "urlopen", side_effect=urlopen
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "missing required field"
                        ):
                            _run(
                                ["--fill-contact", "42", "--vendor", path]
                                + (["--yes"] if yes else [])
                            )
                    self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])
        for flag in (
            "--lines",
            "--vendor-name",
            "--vendor-uid",
            "--file-uuid",
            "--nr",
            "--date",
            "--total",
            "--account",
            "--tax",
            "--currency",
            "--exchange-rate",
            "--due-date",
            "--text",
        ):
            with self.subTest(flag=flag), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as send:
                with self.assertRaisesRegex(SystemExit, "cannot be combined"):
                    _run(
                        [
                            "--fill-contact",
                            "42",
                            "--vendor",
                            self.vendor_file(),
                            flag,
                            "",
                        ]
                    )
                send.assert_not_called()

    def test_timestamp_only_change_refuses_before_post(self):
        before = self.contact()
        path = self.vendor_file()
        urlopen, calls = fake_bexio(
            {
                ("GET", "/2.0/contact/42"): [before, dict(before, updated_at="new")],
            }
        )
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "changed since planning"):
                _run(["--fill-contact", "42", "--vendor", path, "--yes"])
        self.assertEqual(_sent(calls, "POST", "/2.0/contact/42"), [])


class ContactBodyValidationTest(unittest.TestCase):
    def test_contact_body_refuses_unknown_vendor_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            cb.contact_body(dict(VENDOR, unexpected="invented"), 9, 5)

    def test_invalid_vendor_uid_refuses_before_any_request(self):
        path = os.path.join(cb.PRIVATE, "invalid-uid-vendor.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(VENDOR, vat_number="not-a-uid"), f)
        with mock.patch.dict(os.environ, BEXIO_TOKEN=TOKEN), mock.patch.object(
            cb.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaisesRegex(SystemExit, "refused vendor UID"):
                _run(["--vendor", path] + ARGS[2:])
        urlopen.assert_not_called()

    def test_contact_body_uses_vendor_fields_and_owner(self):
        body = cb.contact_body(VENDOR, 9, 5)
        self.assertEqual(body["street_name"], "Beispielweg")
        self.assertEqual(body["house_number"], "12")
        self.assertEqual(body["postcode"], "8000")
        self.assertEqual(body["city"], "Beispielstadt")
        self.assertEqual(body["mail"], "rechnung@example.invalid")
        self.assertEqual(body["url"], "https://example.invalid/vendor")
        self.assertNotIn("country", body)
        self.assertEqual((body["user_id"], body["owner_id"]), (9, 9))
        self.assertTrue(set(body).issubset(cb.CONTACT_CREATE_FIELDS))
        self.assertNotIn("vat_number", body)
        self.assertEqual(body["remarks"], "UID CHE-000.000.001")

    def test_contact_body_without_uid_has_only_documented_fields(self):
        body = cb.contact_body({"name_1": "Muster AG"}, 9, 5)
        self.assertTrue(set(body).issubset(cb.CONTACT_CREATE_FIELDS))
        self.assertNotIn("vat_number", body)
        self.assertNotIn("remarks", body)

    def test_uid_inputs_are_canonicalized(self):
        for value in ("che000000001", "CHE-000.000.001 MWST"):
            self.assertEqual(
                cb.contact_body({"name_1": "Muster AG", "vat_number": value}, 9, 5)[
                    "remarks"
                ],
                "UID CHE-000.000.001",
            )

    def test_invalid_uid_is_refused_before_any_request(self):
        with mock.patch.dict(os.environ, BEXIO_TOKEN=TOKEN), mock.patch.object(
            cb.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaisesRegex(SystemExit, "refused vendor UID"):
                _run(["--vendor-uid", "not-a-uid"] + ARGS[2:])
        urlopen.assert_not_called()


class GeneralizationTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lookup_args(self, *lookup):
        args = list(ARGS[2:])
        return list(lookup) + args

    def test_name_only_reuses_contact_with_uid_in_remarks(self):
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes", "--vendor-name", VENDOR["name_1"]] + ARGS[2:])
        self.assertEqual(_sent(calls, "POST", "/2.0/contact"), [])
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 12
        )

    def test_conflicting_name_uid_and_uid_match_refuse(self):
        answers = standard_answers(
            contacts=[
                {
                    "id": 30,
                    "name_1": VENDOR["name_1"],
                    "remarks": "Other UID CHE-999.999.999",
                },
                {
                    "id": 31,
                    "name_1": "Different Name",
                    "remarks": "legacy UID CHE000000001 MWST",
                },
            ]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "30, 31"):
                _run(["--yes"] + ARGS)
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])

    def test_uid_match_beats_exact_name_without_uid(self):
        answers = standard_answers(
            contacts=[
                {"id": 30, "name_1": VENDOR["name_1"]},
                {
                    "id": 31,
                    "name_1": "Different Name",
                    "remarks": "legacy UID CHE000000001 MWST",
                },
            ]
        )
        del answers[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 31
        )
        self.assertIn("Different Name", out)

    def test_uid_in_remarks_matches_when_name_differs(self):
        answers = standard_answers(
            contacts=[
                {
                    "id": 61,
                    "name_1": "Renamed Muster",
                    "remarks": "Billing UID: CHE 000.000.001 MWST",
                }
            ]
        )
        del answers[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 61
        )

    def test_name_with_conflicting_uid_refuses_before_write(self):
        answers = standard_answers(
            contacts=[
                {
                    "id": 51,
                    "name_1": VENDOR["name_1"],
                    "remarks": "UID CHE-999.999.999",
                },
                {
                    "id": 52,
                    "name_1": "Renamed Vendor",
                    "remarks": "UID CHE-000.000.001",
                },
            ]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("51", str(cm.exception))
        self.assertIn("52", str(cm.exception))
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])

    def test_name_without_remarks_falls_back_to_exact_name(self):
        answers = standard_answers(contacts=[{"id": 62, "name_1": VENDOR["name_1"]}])
        del answers[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 62
        )

    def test_exact_name_reused_but_substring_does_not_match(self):
        exact = standard_answers(
            contacts=[{"id": 41, "name_1": "  MUSTER   REINIGUNG AG "}]
        )
        del exact[("GET", "/2.0/country")]
        urlopen, calls = fake_bexio(exact)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        self.assertEqual(
            _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 41
        )
        args = self.lookup_args("--vendor-name", "Muster Reinigung AG")
        answers = standard_answers(
            contacts=[{"id": 42, "name_1": "Muster Reinigung AG Ost"}]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(args)
        self.assertIn("no contact; pass --vendor <file>", str(cm.exception))
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])

    def test_vendor_file_outside_private_and_unknown_key_refuse(self):
        with mock.patch.object(
            cb, "PRIVATE", "/tmp/bexio-private-test"
        ), mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(["--vendor", "/tmp/outside-vendor.json"] + ARGS[2:])
            urlopen.assert_not_called()
        path = os.path.join(cb.PRIVATE, "vendors", "bad-vendor.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(VENDOR, surprise="no"), f)
        with mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit) as cm:
                _run(["--vendor", path] + ARGS[2:])
        self.assertIn("unknown keys", str(cm.exception))
        urlopen.assert_not_called()

    def test_invalid_accounts_and_taxes_refuse(self):
        bad_accounts = [
            [],
            [{"id": 1, "account_no": "6000", "is_active": False, "is_locked": False}],
            [{"id": 1, "account_no": "6000", "is_active": True, "is_locked": True}],
            [
                {
                    "id": 1,
                    "account_no": "6000",
                    "account_type": 1,
                    "is_active": True,
                    "is_locked": False,
                }
            ],
        ]
        for rows in bad_accounts:
            answers = standard_answers()
            answers[("GET", "/2.0/accounts")] = [rows]
            urlopen, calls = fake_bexio(answers)
            with self.subTest(rows=rows), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ):
                with self.assertRaises(SystemExit):
                    _run(ARGS)
                self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])
        for tax in [
            {"id": 1, "code": "VM81", "type": "sales_tax", "is_active": True},
            {"id": 1, "code": "VM81", "type": "pre_tax_material", "is_active": False},
        ]:
            answers = standard_answers()
            answers[("GET", "/3.0/taxes")] = [[tax]]
            urlopen, calls = fake_bexio(answers)
            with self.subTest(tax=tax), mock.patch.object(
                cb.urllib.request, "urlopen", side_effect=urlopen
            ):
                with self.assertRaises(SystemExit):
                    _run(ARGS)
                self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])

    def test_same_vendor_ref_for_other_supplier_warns_and_exact_request_set(self):
        answers = standard_answers(
            bills=[{"id": 9, "supplier_id": 999, "vendor_ref": "INV-TEST-1"}]
        )
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(ARGS)
        self.assertIn("warning: vendor_ref INV-TEST-1 exists for another supplier", out)
        self.assertEqual([key for key, _ in calls if key[0] == "POST"], [])
        self.assertEqual(
            cb.REQUESTS,
            {
                ("GET", "/2.0/contact"),
                ("GET", "/2.0/country"),
                ("GET", "/3.0/users/me"),
                ("GET", "/4.0/purchase/bills"),
                ("GET", "/2.0/accounts"),
                ("GET", "/3.0/taxes"),
                ("POST", "/2.0/contact"),
                ("POST", "/4.0/purchase/bills"),
            },
        )


class ReviewRegressionTest(unittest.TestCase):
    setUp = GeneralizationTest.setUp
    lookup_args = GeneralizationTest.lookup_args

    def test_duplicate_check_uses_supplier_filter_and_documented_limit(self):
        # Actual list rows omit supplier_id, even for a matching supplier.
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        row = {"id": "invented-bill", "vendor_ref": "INV-TEST-1"}
        answers[("GET", "/4.0/purchase/bills")] = [
            {"data": [row]},
            {"data": [row]},
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "already exists for this supplier"):
                _run(["--yes"] + ARGS)
        queries = [
            urllib.parse.parse_qs(urllib.parse.urlparse(r.full_url).query)
            for r in _sent(calls, "GET", "/4.0/purchase/bills")
        ]
        self.assertEqual(
            queries,
            [
                {"page": ["1"], "limit": ["500"]},
                {"page": ["1"], "limit": ["500"], "supplier_id": ["12"]},
            ],
        )
        self.assertFalse(any(key[0] == "POST" for key, _ in calls))

    def test_lookup_address_uses_current_contact_and_country_id(self):
        existing = dict(
            CONTACT,
            id=12,
            name_1="Current Invented Name",
            country_id=6,
            contact_type_id=2,
        )
        del existing["country"]
        answers = standard_answers(contacts=[existing])
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + self.lookup_args("--vendor-uid", VENDOR["vat_number"]))
        address = _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["address"]
        self.assertEqual(address["lastname_company"], "Current Invented Name")
        self.assertEqual(address["type"], "PRIVATE")
        self.assertEqual(address["country_code"], "DE")
        first_post = next(i for i, (key, _) in enumerate(calls) if key[0] == "POST")
        self.assertTrue(all(key[0] == "GET" for key, _ in calls[:first_post]))
        self.assertTrue(all(key[0] in {"POST", "GET"} for key, _ in calls[first_post:]))
        self.assertEqual(
            len(_sent(calls, "GET", "/4.0/purchase/bills/" + FAKE_BILL_ID)), 1
        )

    def test_minimal_address_has_required_type_and_omits_empty_fields(self):
        self.assertEqual(
            cb.contact_address({"name_1": "Invented Supplier"}),
            {
                "lastname_company": "Invented Supplier",
                "type": "COMPANY",
            },
        )

    def test_foreign_currency_requires_rate_before_any_request(self):
        for rate in [None, "invalid", "NaN", "0", "-1"]:
            args = ["--currency", "EUR"] + ARGS
            if rate is not None:
                args += ["--exchange-rate", rate]
            with self.subTest(rate=rate), mock.patch.object(
                cb.urllib.request, "urlopen"
            ) as send:
                with self.assertRaises(SystemExit):
                    _run(["--yes"] + args)
                send.assert_not_called()

    def test_foreign_currency_sends_required_conversion_fields(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes", "--currency", "EUR", "--exchange-rate", "1"] + ARGS)
        bill = _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])
        self.assertEqual(bill["currency_code"], "EUR")
        self.assertEqual(bill["exchange_rate"], 1)
        self.assertEqual(bill["base_currency_amount"], bill["amount_calc"])

    def test_other_supplier_reference_warns_with_realistic_list_rows(self):
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        answers[("GET", "/4.0/purchase/bills")] = [
            {"data": [{"id": "invented-bill", "vendor_ref": "INV-TEST-1"}]},
            {"data": []},
        ]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        self.assertIn("exists for another supplier", out)
        self.assertEqual(len(_sent(calls, "POST", "/4.0/purchase/bills")), 1)

    def test_vendor_field_types_refuse_before_any_request(self):
        path = os.path.join(cb.PRIVATE, "bad-vendor-type.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(VENDOR, house_number=12), f)
        with mock.patch.object(cb.urllib.request, "urlopen") as send:
            with self.assertRaisesRegex(SystemExit, "house_number must be a string"):
                _run(["--yes", "--vendor", path] + ARGS[2:])
            send.assert_not_called()

    def test_accounts_are_paged_and_unknown_tax_refuses(self):
        answers = standard_answers()
        answers[("GET", "/2.0/accounts")] = [
            [{"account_no": "other"}] * cb.PAGE_SIZE,
            answers[("GET", "/2.0/accounts")][0],
        ]
        answers[("GET", "/3.0/taxes")] = [[]]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(SystemExit, "unknown tax code"):
                _run(["--yes"] + ARGS)
        offsets = [
            urllib.parse.parse_qs(urllib.parse.urlparse(r.full_url).query)["offset"]
            for r in _sent(calls, "GET", "/2.0/accounts")
        ]
        self.assertEqual(offsets, [["0"], ["500"]])
        self.assertFalse(any(key[0] == "POST" for key, _ in calls))


if __name__ == "__main__":
    unittest.main()
