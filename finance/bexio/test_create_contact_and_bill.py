"""Offline tests for create_contact_and_bill.py: the reviewed requests, the plans, the guards, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import create_contact_and_bill as cb

TOKEN = "tok-invented-not-real"
ARGS = ["--file-uuid", "uuid-invented-1", "--nr", "INV-TEST-1", "--date", "2026-01-15", "--total", "733.30"]


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
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cb.main(argv)
    return out.getvalue()


def _sent(calls, method, path):
    return [req for (key, req) in calls if key == (method, path)]


def _body(req):
    return json.loads(req.data.decode("utf-8"))


def standard_answers(contacts=None, bills=None):
    """A yard with no Vebego contact and no bill of that number yet, unless the caller says otherwise."""
    return {
        ("GET", "/2.0/contact"): [contacts if contacts is not None else [{"id": 1, "name_1": "Muster AG"}]],
        ("GET", "/2.0/country"): [[{"id": 5, "iso_3166_alpha2": "CH"}, {"id": 6, "iso_3166_alpha2": "DE"}]],
        ("GET", "/3.0/users/me"): [{"id": 9}],
        ("GET", "/4.0/purchase/bills"): [{"data": bills if bills is not None else []}],
        ("POST", "/2.0/contact"): [{"id": 77}],
        ("POST", "/4.0/purchase/bills"): [{"id": 88}],
    }


class TokenTest(unittest.TestCase):
    def test_refuses_without_the_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit) as cm:
                _run(ARGS)
        self.assertIn("BEXIO_TOKEN", str(cm.exception))
        urlopen.assert_not_called()

    def test_refuses_to_run_through_the_broker(self):
        with mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN, "BEXIO_BROKER": "1"}), \
                mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(ARGS)
        urlopen.assert_not_called()


class PlanTest(unittest.TestCase):
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
        self.assertIn('"name_1": "Vebego AG"', out)
        self.assertIn('"country_id": 5', out)
        self.assertIn('"owner_id": 9', out)
        self.assertIn('"attachment_ids": [\n    "uuid-invented-1"', out)
        self.assertIn('"gross": 733.3', out)
        self.assertNotIn(TOKEN, out)

    def test_yes_creates_the_contact_then_the_bill(self):
        urlopen, calls = fake_bexio(standard_answers())
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        contact = _body(_sent(calls, "POST", "/2.0/contact")[0])
        self.assertEqual(contact["name_1"], "Vebego AG")
        self.assertEqual(contact["contact_type_id"], 1)
        self.assertEqual(contact["country_id"], 5)
        self.assertEqual(contact["vat_number"], "CHE-105.953.190")
        bill = _body(_sent(calls, "POST", "/4.0/purchase/bills")[0])
        self.assertEqual(bill["supplier_id"], 77)
        self.assertEqual(bill["vendor_ref"], "INV-TEST-1")
        self.assertEqual(bill["bill_date"], "2026-01-15")
        self.assertEqual(bill["currency_code"], "CHF")
        self.assertEqual(bill["gross"], 733.3)
        self.assertEqual(bill["title"], cb.DEFAULT_TEXT)
        self.assertEqual(bill["attachment_ids"], ["uuid-invented-1"])
        self.assertEqual(bill["status"], "DRAFT")
        self.assertNotIn("positions", bill)
        self.assertNotIn("line_items", bill)
        self.assertIn("created bexio contact id 77", out)
        self.assertIn("created bexio bill id 88", out)
        self.assertNotIn(TOKEN, out)

    def test_an_existing_contact_is_found_and_not_created(self):
        contacts = [{"id": i, "name_1": "Kunde {}".format(i)} for i in range(cb.PAGE_SIZE)]
        answers = standard_answers()
        answers[("GET", "/2.0/contact")] = [contacts, [{"id": 12, "name_1": "VEBEGO AG"}]]
        del answers[("GET", "/2.0/country")], answers[("GET", "/3.0/users/me")]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            out = _run(["--yes"] + ARGS)
        self.assertEqual(_sent(calls, "POST", "/2.0/contact"), [])
        self.assertEqual(_body(_sent(calls, "POST", "/4.0/purchase/bills")[0])["supplier_id"], 12)
        self.assertIn("found 'VEBEGO AG' as bexio contact id 12; not created", out)

    def test_two_matching_contacts_refuse_before_any_write(self):
        answers = standard_answers(contacts=[{"id": 3, "name_1": "Vebego AG"}, {"id": 4, "name_1": "Vebego Ost"}])
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("2 contacts match", str(cm.exception))
        self.assertEqual([k for (k, _) in calls if k[0] == "POST"], [])

    def test_an_invoice_number_already_in_bexio_refuses_before_any_write(self):
        urlopen, calls = fake_bexio(standard_answers(bills=[{"id": 5, "vendor_ref": "INV-TEST-1"}]))
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes"] + ARGS)
        self.assertIn("already in bexio", str(cm.exception))
        self.assertEqual([k for (k, _) in calls if k[0] == "POST"], [])

    def test_bills_are_read_page_by_page(self):
        full = [{"id": i, "vendor_ref": "other-{}".format(i)} for i in range(cb.PAGE_SIZE)]
        answers = standard_answers()
        answers[("GET", "/4.0/purchase/bills")] = [{"data": full}, {"data": [{"id": 6, "vendor_ref": "INV-TEST-1"}]}]
        urlopen, calls = fake_bexio(answers)
        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(SystemExit):
                _run(["--yes"] + ARGS)
        pages = [urllib.parse.parse_qs(urllib.parse.urlparse(r.full_url).query)["page"] for r in _sent(calls, "GET", "/4.0/purchase/bills")]
        self.assertEqual(pages, [["1"], ["2"]])


class PostGuardTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_contact_post_is_not_retried(self):
        answers = standard_answers()
        answers[("POST", "/2.0/contact")] = [urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b'{"error": "invented"}'))]
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
        answers[("POST", "/4.0/purchase/bills")] = [urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": "invented"}'))]
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
            with self.subTest(method=method, path=path), mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
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
        base = ["--yes", "--file-uuid", "uuid-invented-1", "--nr", "INV-TEST-1"]
        cases = [base + ["--date", "2026-01-15", "--total", t] for t in bad_totals]
        cases += [base + ["--date", d, "--total", "733.30"] for d in bad_dates]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(SystemExit):
                    _run(argv)
                urlopen.assert_not_called()

    def test_empty_invoice_number_sends_nothing(self):
        with mock.patch.object(cb.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(["--yes", "--file-uuid", "uuid-invented-1", "--nr", " ", "--date", "2026-01-15", "--total", "733.30"])
        urlopen.assert_not_called()


class ContactBodyTest(unittest.TestCase):
    def test_the_vendor_has_the_address_from_its_impressum(self):
        body = cb.contact_body(9, 5)
        self.assertEqual(body["street_name"], "Albisriederstrasse")
        self.assertEqual(body["house_number"], "253")
        self.assertEqual(body["postcode"], "8047")
        self.assertEqual(body["city"], "Zürich")
        self.assertEqual((body["user_id"], body["owner_id"]), (9, 9))


if __name__ == "__main__":
    unittest.main()
