"""Offline boundary tests for the reviewed sales invoice writer."""

import json
import os
import stat
import unittest
import urllib.parse
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(os.path.abspath(__file__))
si = launcher.load("create_sales_invoice.py")
from private_guard import setUpModule  # noqa: E402

# These accepted and required fields follow the published docs and are not proven live.
DOC_FIELDS = {
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
DOC_REQUIRED = {
    "title",
    "contact_id",
    "user_id",
    "language_id",
    "bank_account_id",
    "currency_id",
    "payment_type_id",
    "mwst_type",
    "mwst_is_net",
    "is_valid_from",
    "is_valid_to",
    "positions",
}


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def fake_bexio():
    calls = []

    def urlopen(req, timeout=None):
        parsed = urllib.parse.urlparse(req.full_url)
        method, path = req.get_method(), parsed.path
        calls.append((method, path, req))
        if method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            missing = DOC_REQUIRED - set(body)
            unknown = set(body) - DOC_FIELDS
            if missing or unknown:
                raise AssertionError("fake bexio rejects request fields")
            raise AssertionError("dry run must not POST")
        responses = {
            "/2.0/contact/42": {"id": 42, "name_1": "Invented Customer"},
            "/3.0/users/me": {"id": 9},
            "/2.0/language": [{"id": 1, "name": "English"}],
            "/2.0/payment_type": [{"id": 1, "name": "Bank transfer"}],
            "/3.0/banking/accounts": [{"id": 1, "name": "Main account"}],
            "/3.0/currencies": [{"id": 1, "name": "CHF"}],
            "/2.0/accounts": [
                {
                    "id": 30,
                    "account_no": "3000",
                    "is_active": True,
                    "is_locked": False,
                    "account_type": 1,
                }
            ],
            "/3.0/taxes": [{"id": 8, "code": "VAT81"}],
            "/2.0/unit": [{"id": 5, "name": "h"}],
            "/2.0/kb_invoice": [],
        }
        return _Response(responses[path])

    return urlopen, calls


class LauncherTest(unittest.TestCase):
    PATH = os.path.join(
        os.path.dirname(os.path.abspath(si.__file__)), "create_sales_invoice.py"
    )

    def test_head_mode_and_retired_name(self):
        with open(self.PATH, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(
            content.splitlines()[0], "#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash"
        )
        self.assertTrue(os.stat(self.PATH).st_mode & stat.S_IXUSR)
        self.assertNotIn("varlock", content.lower())
        self.assertIn("<<'__BEXIO_SALES_INVOICE_PY__'", content)


class RequestBoundaryTest(unittest.TestCase):
    def test_only_single_invoice_post_and_read_routes_are_allowed(self):
        for method, path in (
            ("POST", "/2.0/kb_invoice/1"),
            ("PUT", "/2.0/kb_invoice/1"),
            ("DELETE", "/2.0/kb_invoice/1"),
            ("POST", "/2.0/kb_invoice/1/issue"),
            ("POST", "/2.0/kb_invoice/1/send"),
            ("POST", "/2.0/kb_invoice/1/mark_as_sent"),
            ("POST", "/2.0/kb_invoice/1/payment"),
            ("POST", "/2.0/kb_invoice/search"),
            ("PUT", "/2.0/kb_invoice"),
            ("GET", "/2.0/contact/nope"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(
                si.RefusedRequest
            ):
                si._send(method, path, "invented")


class FileValidationTest(unittest.TestCase):
    def test_no_token_stops_before_network(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            si.urllib.request, "urlopen"
        ) as send:
            with self.assertRaises(SystemExit) as error:
                si.main(["--invoice", "ignored"])
        self.assertIn("BEXIO_TOKEN", str(error.exception))
        send.assert_not_called()

    def test_private_path_and_strict_input_keys(self):
        path = os.path.join(si.PRIVATE, "sales.json")
        os.makedirs(si.PRIVATE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"unexpected": 1}, f)
        with self.assertRaises(SystemExit):
            si.load_invoice(path)
        with self.assertRaises(SystemExit):
            si.load_invoice(os.path.join(os.path.dirname(si.PRIVATE), "outside.json"))

    def test_decimal_strings_must_be_positive_and_bounded(self):
        for value in (1, "0", "-1", "0.1234567", "NaN", "1e2"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                si._positive(value, "amount")
        self.assertEqual(str(si._positive("1000.00", "amount")), "1000.00")

    def test_fake_contract_detects_missing_required_field(self):
        body = {
            "title",
            "contact_id",
            "user_id",
            "language_id",
            "bank_account_id",
            "currency_id",
            "payment_type_id",
            "mwst_type",
            "mwst_is_net",
            "is_valid_from",
            "is_valid_to",
            "positions",
        }
        self.assertFalse(DOC_REQUIRED - body)
        self.assertTrue(DOC_REQUIRED - (body - {"contact_id"}))
        self.assertFalse(body - DOC_FIELDS)

    def test_dry_run_resolves_plan_without_post_or_result_file(self):
        os.makedirs(si.PRIVATE, exist_ok=True)
        path = os.path.join(si.PRIVATE, "invoice.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "contact_id": 42,
                    "title": "Invented work",
                    "date": "2026-01-15",
                    "due_date": "2026-02-14",
                    "vat_mode": "net",
                    "bank_account_id": 1,
                    "payment_type_id": 1,
                    "language_id": 1,
                    "currency_id": 1,
                    "positions": [
                        {
                            "text": "Invented service",
                            "amount": "1.00",
                            "unit_price": "1000.00",
                            "account": "3000",
                            "tax": "VAT81",
                            "unit": "h",
                        }
                    ],
                },
                f,
            )
        urlopen, calls = fake_bexio()
        out = __import__("io").StringIO()
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            si.urllib.request, "urlopen", side_effect=urlopen
        ), mock.patch.object(
            si, "_record", side_effect=AssertionError("dry run wrote a result")
        ), mock.patch(
            "sys.stdout", out
        ):
            si.main(["--invoice", path])
        self.assertIn("dry run: nothing created", out.getvalue())
        self.assertTrue(all(method == "GET" for method, _, _ in calls))
