"""Offline tests for create_contact_and_bill.py: the reviewed requests, the plans, the guards, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
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
        ("POST", "/4.0/purchase/bills"): [{"id": 88}],
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
        self.assertIn("bill id 88", content)
        self.assertIn("vendor_ref INV-TEST-1 supplier id 77", content)
        self.assertNotIn(TOKEN, content)

    def test_an_existing_contact_records_only_the_bill_id(self):
        answers = standard_answers(contacts=[dict(CONTACT, id=12)])
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            _run(["--yes"] + ARGS)
        content = self.result()
        self.assertNotIn("contact id 77", content)
        self.assertIn("bill id 88", content)

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
        self.assertIn('"amount_man": 733.3', out)
        self.assertIn('"booking_account_id": 16', out)
        self.assertIn('"tax_id": 23', out)
        self.assertNotIn(TOKEN, out)


class ReverseChargeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with_tax(self, code, tax_type="pre_tax_material", active=True):
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
                self.assertIn('"amount_man": 733.3', out)
                self.assertIn('"manual_amount": true', out)
                bill_json = out.split('"line_items": [', 1)[1]
                self.assertIn('"amount": 733.3', bill_json)
                self.assertEqual(_sent(calls, "POST", "/4.0/purchase/bills"), [])

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
        self.assertEqual(bill["vendor_ref"], "INV-TEST-1")
        self.assertEqual(bill["bill_date"], "2026-01-15")
        self.assertEqual(bill["currency_code"], "CHF")
        self.assertEqual(bill["amount_man"], 733.3)
        self.assertEqual(bill["title"], "Invented supplier service")
        self.assertEqual(bill["attachment_ids"], ["uuid-invented-1"])
        self.assertNotIn("status", bill)
        self.assertEqual(bill["line_items"][0]["amount"], 733.3)
        self.assertEqual(bill["line_items"][0]["booking_account_id"], 16)
        self.assertEqual(bill["line_items"][0]["tax_id"], 23)
        self.assertEqual(bill["currency_code"], "CHF")
        self.assertEqual(bill["due_date"], "2026-02-14")
        self.assertFalse(bill["item_net"])
        self.assertTrue(bill["manual_amount"])
        self.assertEqual(bill["discounts"], [])
        self.assertNotIn("split_into_line_items", bill)
        self.assertNotIn("status", bill)
        self.assertIn("created bexio contact id 77", out)
        self.assertIn("created bexio bill id 88", out)
        self.assertNotIn(TOKEN, out)

    def test_an_existing_contact_is_found_and_not_created(self):
        contacts = [{"id": i, "name_1": f"Kunde {i}"} for i in range(cb.PAGE_SIZE)]
        answers = standard_answers()
        answers[("GET", "/2.0/contact")] = [
            contacts,
            [dict(CONTACT, id=12)],
        ]
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
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
            ("PUT", "/4.0/purchase/bills/88"),
            ("DELETE", "/2.0/contact/77"),
            ("POST", "/2.0/contact/search"),
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
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
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
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
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
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
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
        del exact[("GET", "/2.0/country")], exact[("GET", "/3.0/users/me")]
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
        self.assertTrue(all(key[0] == "POST" for key, _ in calls[first_post:]))

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
        self.assertEqual(bill["base_currency_amount"], bill["amount_man"])

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
