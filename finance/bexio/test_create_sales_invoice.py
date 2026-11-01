"""Offline boundary tests for the reviewed sales invoice writer."""

import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal
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


DOC_POSITION_FIELDS = {
    "type",
    "amount",
    "unit_price",
    "account_id",
    "tax_id",
    "text",
    "unit_id",
}
DOC_POSITION_REQUIRED = DOC_POSITION_FIELDS - {"unit_id"}


def http_error(req, code):
    return urllib.error.HTTPError(
        req.full_url,
        code,
        "invented refusal",
        {},
        io.BytesIO(b'{"message": "invented refusal detail"}'),
    )


def fake_bexio():
    calls = []
    stored = []
    responses = {
        "/2.0/contact/42": {"id": 42, "name_1": "Invented Customer"},
        "/2.0/contact/43": {"id": 43, "name_1": "Invented Subcontact"},
        "/3.0/users/me": {"id": 9},
        "/2.0/language": [{"id": 1, "name": "English"}],
        "/2.0/payment_type": [{"id": 1, "name": "Bank transfer"}],
        "/3.0/banking/accounts": [{"id": 1, "name": "Invented bank"}],
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

    def urlopen(req, timeout=None):
        parsed = urllib.parse.urlparse(req.full_url)
        method, path = req.get_method(), parsed.path
        calls.append((method, path, req))
        if method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            positions = body.get("positions")
            if (
                DOC_REQUIRED - set(body)
                or set(body) - DOC_FIELDS
                or not isinstance(positions, list)
                or not positions
            ):
                raise http_error(req, 422)
            for row in positions:
                if (
                    DOC_POSITION_REQUIRED - set(row)
                    or set(row) - DOC_POSITION_FIELDS
                    or row.get("type") != "KbPositionCustom"
                    or any(
                        not isinstance(row.get(k), str)
                        for k in ("amount", "unit_price")
                    )
                ):
                    raise http_error(req, 422)
            actual = copy.deepcopy(body)
            actual.update(
                id=701,
                document_nr="INVENTED-DRAFT",
                kb_item_status_id=7,
                header="Invented header",
                footer=None,
                template_slug=None,
                total="invented server total",
                total_taxes="invented server taxes",
            )
            for row in actual["positions"]:
                row["position_total"] = str(
                    (Decimal(row["amount"]) * Decimal(row["unit_price"])).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                row["amount"] = format(Decimal(row["amount"]), ".6f")
                row["unit_price"] = format(Decimal(row["unit_price"]), ".6f")
            stored.append(actual)
            return _Response({"id": 701, "document_nr": "INVENTED-DRAFT"})
        if path == "/2.0/kb_invoice/701":
            assert stored, "read-back before creation"
            return _Response(stored[-1])
        value = copy.deepcopy(responses[path])
        if isinstance(value, list):
            query = urllib.parse.parse_qs(parsed.query)
            offset = int(query.get("offset", [0])[0])
            limit = int(query.get("limit", [500])[0])
            value = value[offset : offset + limit]
        return _Response(value)

    urlopen.responses = responses
    urlopen.stored = stored
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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sales-invoice-file-test-")
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(si, "PRIVATE", self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)

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
        self.assertEqual(str(si._positive("6739.428516", "amount")), "6739.428516")

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
                            "amount": "2",
                            "unit_price": "6739.428516",
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
            si, "record_result", side_effect=AssertionError("dry run wrote a result")
        ), mock.patch(
            "sys.stdout", out
        ):
            si.main(["--invoice", path])
        self.assertIn("dry run: nothing created", out.getvalue())
        self.assertTrue(all(method == "GET" for method, _, _ in calls))


def invoice():
    return {
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
                "amount": "2",
                "unit_price": "6739.428516",
                "account": "3000",
                "tax": "VAT81",
            }
        ],
    }


class CreationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sales-invoice-test-")
        self.addCleanup(self.temp.cleanup)
        self.private = self.temp.name
        patch = mock.patch.object(si, "PRIVATE", self.private)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake, self.calls = fake_bexio()
        self.output = io.StringIO()

    def run_invoice(self, data=None, yes=True, transport=None):
        path = os.path.join(self.private, "invoice.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump(invoice() if data is None else data, out)
        args = ["--invoice", path] + (["--yes"] if yes else [])
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            si.urllib.request, "urlopen", side_effect=transport or self.fake
        ), contextlib.redirect_stdout(
            self.output
        ):
            si.main(args)

    def result(self):
        path = os.path.join(self.private, si.RESULT)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as source:
            return source.read()

    def assert_no_post(self):
        self.assertFalse(any(method != "GET" for method, _, _ in self.calls))
        self.assertEqual(self.result(), "")

    def test_happy_paths_all_vat_modes_and_optional_fields(self):
        for mode, expected in (
            ("net", (0, True)),
            ("gross", (0, False)),
            ("exempt", (2, True)),
        ):
            for optional in (False, True):
                with self.subTest(mode=mode, optional=optional):
                    self.fake, self.calls = fake_bexio()
                    data = invoice()
                    data["vat_mode"] = mode
                    if optional:
                        data["contact_sub_id"] = 43
                        data["positions"][0]["unit"] = "h"
                        row = copy.deepcopy(data["positions"][0])
                        row["text"] = "Invented second row"
                        data["positions"].append(row)
                    self.run_invoice(data)
                    posts = [req for method, _, req in self.calls if method == "POST"]
                    self.assertEqual(len(posts), 1)
                    body = json.loads(posts[0].data)
                    self.assertEqual((body["mwst_type"], body["mwst_is_net"]), expected)
                    self.assertEqual(len(body["positions"]), 2 if optional else 1)
                    self.assertEqual("contact_sub_id" in body, optional)
                    self.assertEqual("unit_id" in body["positions"][0], optional)
                    self.assertEqual(self.calls[-1][:2], ("GET", "/2.0/kb_invoice/701"))
                    self.assertIn("read-back pending", self.result())
                    self.assertIn(
                        "document_nr=yes header=yes footer=no template_slug=no",
                        self.result(),
                    )
                    self.assertNotIn("Invented Customer", self.result())
                    self.assertNotIn("invented-token", self.result())
                    self.assertIn("invented server total", self.output.getvalue())
                    if optional:
                        self.assertIn("Invented Subcontact", self.output.getvalue())

    def test_dry_run_creates_no_files_and_checks_tax_query(self):
        self.run_invoice(yes=False)
        self.assert_no_post()
        self.assertEqual(os.listdir(self.private), ["invoice.json"])
        tax_req = next(req for _, path, req in self.calls if path == "/3.0/taxes")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(tax_req.full_url).query)
        self.assertEqual(query["scope"], ["active"])
        self.assertEqual(query["types"], ["sales_tax"])
        self.assertEqual(query["date"], [invoice()["date"]])

    def test_every_input_refusal_prevents_all_requests(self):
        cases = [None, [], {"unknown": 1}]
        original = invoice()
        for key in original:
            item = copy.deepcopy(original)
            del item[key]
            cases.append(item)
        for key, values in {
            "title": ["", "  ", None, 3],
            "date": ["bad", None],
            "due_date": ["2025-01-01", "bad"],
            "vat_mode": ["other", 1, [], {}],
            "positions": [[], [{}] * 101, {}, [None]],
            **{
                k: [0, -1, True, "1", None]
                for k in (
                    "contact_id",
                    "contact_sub_id",
                    "bank_account_id",
                    "payment_type_id",
                    "language_id",
                    "currency_id",
                )
            },
        }.items():
            for value in values:
                item = copy.deepcopy(original)
                item[key] = value
                cases.append(item)
        item = copy.deepcopy(original)
        item["unknown"] = 1
        cases.append(item)
        for key in original["positions"][0]:
            item = copy.deepcopy(original)
            del item["positions"][0][key]
            cases.append(item)
        for key, values in {
            "text": ["", "  ", None],
            "account": ["", 3000],
            "tax": ["", 8],
            "unit": ["", 5],
            "unexpected": ["no"],
            "amount": [0, "0", "-1", "NaN", "1e2", "0.1234567"],
            "unit_price": [0, "0", "-1", "NaN", "1e2", "0.1234567"],
        }.items():
            for value in values:
                item = copy.deepcopy(original)
                item["positions"][0][key] = value
                cases.append(item)
        for data in cases:
            if (
                data is None
            ):  # run_invoice's None means its default; use JSON null directly below
                continue
            with self.subTest(data=data), self.assertRaises(SystemExit):
                self.run_invoice(data)
            self.assertEqual(self.calls, [])
        self.assert_no_post()

    def test_private_realpath_guard_rejects_symlink_escape_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "invoice.json")
            with open(target, "w") as out:
                out.write("null")
            path = os.path.join(self.private, "escape.json")
            os.symlink(target, path)
            with self.assertRaises(SystemExit):
                si.load_invoice(path)
        path = os.path.join(self.private, "bad.json")
        for text in ("{", "null"):
            with open(path, "w") as out:
                out.write(text)
            with self.assertRaises(SystemExit):
                si.load_invoice(path)
        self.assert_no_post()

    def test_reference_refusals_prevent_post(self):
        cases = []
        for path in ("/2.0/contact/42", "/2.0/contact/43", "/3.0/users/me"):
            cases.append((path, None))
        for path in (
            "/2.0/language",
            "/2.0/payment_type",
            "/3.0/banking/accounts",
            "/3.0/currencies",
            "/2.0/accounts",
            "/3.0/taxes",
            "/2.0/unit",
        ):
            rows = self.fake.responses[path]
            cases.extend(((path, []), (path, rows + rows)))
        for key, value in (
            ("is_active", False),
            ("is_locked", True),
            ("account_type", 2),
        ):
            rows = copy.deepcopy(self.fake.responses["/2.0/accounts"])
            rows[0][key] = value
            cases.append(("/2.0/accounts", rows))
        for path, value in cases:
            self.fake, self.calls = fake_bexio()
            self.fake.responses[path] = value
            data = invoice()
            data["contact_sub_id"] = 43
            data["positions"][0]["unit"] = "h"
            with self.subTest(path=path, value=value), self.assertRaises(SystemExit):
                self.run_invoice(data)
            self.assert_no_post()

    def test_duplicate_on_later_page_prevents_post(self):
        self.fake.responses["/2.0/kb_invoice"] = [
            {
                "contact_id": 999,
                "title": "Invented unrelated",
                "is_valid_from": "2024-01-01",
            }
        ] * si.PAGE_SIZE + [
            {
                "contact_id": 42,
                "title": invoice()["title"],
                "is_valid_from": invoice()["date"],
            }
        ]
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.run_invoice()
        self.assert_no_post()
        self.assertEqual(sum(path == "/2.0/kb_invoice" for _, path, _ in self.calls), 2)

    def test_fake_rejects_each_missing_or_unknown_field_and_numeric_position(self):
        self.run_invoice()
        good = json.loads(
            next(req.data for method, _, req in self.calls if method == "POST")
        )
        self.fake.stored.clear()
        bad = []
        for key in DOC_REQUIRED:
            body = copy.deepcopy(good)
            del body[key]
            bad.append(body)
        for key in (
            "unknown",
            "kb_item_status_id",
            "document_nr",
            "header",
            "footer",
            "template_slug",
            "reference",
            "api_reference",
            "id",
            "total",
        ):
            body = copy.deepcopy(good)
            body[key] = 7
            bad.append(body)
        for key in DOC_POSITION_REQUIRED:
            body = copy.deepcopy(good)
            del body["positions"][0][key]
            bad.append(body)
        for key in ("amount", "unit_price", "id", "position_total", "unknown"):
            body = copy.deepcopy(good)
            body["positions"][0][key] = 5
            bad.append(body)
        for body in bad:
            with self.subTest(body=body), mock.patch.object(
                si.urllib.request, "urlopen", side_effect=self.fake
            ):
                with self.assertRaisesRegex(SystemExit, "HTTP 422"):
                    si._send("POST", "/2.0/kb_invoice", "invented-token", body=body)
            self.assertEqual(self.fake.stored, [])

    def test_http_read_refusal_has_status_and_detail(self):
        def transport(req, timeout=None):
            self.calls.append(
                (req.get_method(), urllib.parse.urlparse(req.full_url).path, req)
            )
            raise http_error(req, 403)

        with self.assertRaisesRegex(SystemExit, "HTTP 403.*invented refusal detail"):
            self.run_invoice(transport=transport)
        self.assert_no_post()
        self.assertEqual(len(self.calls), 1)

    def test_post_refusals_and_uncertain_responses_are_never_retried(self):
        for failure in ("403", "timeout", "urlerror", "bad-json", "no-id"):
            self.fake, self.calls = fake_bexio()

            def transport(req, timeout=None):
                if req.get_method() != "POST":
                    return self.fake(req, timeout)
                self.calls.append(("POST", "/2.0/kb_invoice", req))
                if failure == "403":
                    raise http_error(req, 403)
                if failure == "timeout":
                    raise TimeoutError("invented timeout")
                if failure == "urlerror":
                    raise urllib.error.URLError("invented connection failure")
                if failure == "bad-json":
                    response = _Response(None)
                    response.body = b"{"
                    return response
                return _Response({})

            message = (
                "kb_invoice_edit"
                if failure == "403"
                else "check bexio before running again"
            )
            with self.subTest(failure=failure), self.assertRaisesRegex(
                SystemExit, message
            ):
                self.run_invoice(transport=transport)
            self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)
            self.assertEqual(self.result(), "")
            self.assertEqual(self.calls[-1][0], "POST")

    def test_failed_readback_always_records_existing_draft(self):
        for failure in (
            "status",
            "row",
            "count",
            "order",
            "type",
            "decimal",
            "total",
            "http",
            "timeout",
            "bad-row",
            "unit",
        ):
            self.fake, self.calls = fake_bexio()

            def transport(req, timeout=None):
                if req.full_url.endswith("/701"):
                    self.assertIn("read-back pending", self.result())
                    if failure == "http":
                        raise http_error(req, 503)
                    if failure == "timeout":
                        raise TimeoutError("invented timeout")
                    actual = self.fake.stored[-1]
                    if failure == "status":
                        actual["kb_item_status_id"] = 3
                    elif failure == "count":
                        actual["positions"].pop()
                    elif failure == "order":
                        actual["positions"].reverse()
                    elif failure == "type":
                        actual["positions"][0]["type"] = "KbPositionArticle"
                    elif failure == "decimal":
                        actual["positions"][0]["amount"] = "invalid"
                    elif failure == "total":
                        actual["positions"][0]["position_total"] = "0"
                    elif failure == "bad-row":
                        actual["positions"][0] = None
                    elif failure == "unit":
                        actual["positions"][0]["unit_id"] = 999
                    else:
                        actual["positions"][0]["text"] = "Invented changed row"
                return self.fake(req, timeout)

            data = invoice()
            if failure == "unit":
                data["positions"][0]["unit"] = "h"
            second = copy.deepcopy(data["positions"][0])
            second["text"] = "Invented second row"
            data["positions"].append(second)
            with self.subTest(failure=failure), self.assertRaisesRegex(
                SystemExit, "draft id 701 exists.*do not run again"
            ):
                self.run_invoice(data, transport=transport)
            self.assertIn("read-back failed", self.result())
            self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)

    def test_result_write_failure_warns_draft_exists(self):
        with mock.patch.object(
            si, "record_result", side_effect=OSError("invented disk failure")
        ):
            with self.assertRaisesRegex(
                SystemExit, "draft id 701 exists.*do not run again"
            ):
                self.run_invoice()
        self.assertEqual(self.calls[-1][0], "POST")

    def test_position_limit_and_same_day_due_date_are_accepted(self):
        data = invoice()
        data["due_date"] = data["date"]
        data["positions"] = data["positions"] * 100
        self.run_invoice(data)
        self.assertEqual(len(self.fake.stored[0]["positions"]), 100)

    def test_header_and_row_changes_fail_readback(self):
        self.run_invoice()
        expected = json.loads(
            next(req.data for method, _, req in self.calls if method == "POST")
        )
        actual = self.fake.stored[0]
        for key in (
            "contact_id",
            "contact_sub_id",
            "title",
            "is_valid_from",
            "is_valid_to",
            "mwst_type",
            "mwst_is_net",
            "bank_account_id",
            "payment_type_id",
            "language_id",
            "currency_id",
        ):
            changed = copy.deepcopy(actual)
            changed[key] = "invented changed value"
            with self.subTest(key=key), self.assertRaises(ValueError):
                si._verify(expected, changed)
        for key in (
            "text",
            "account_id",
            "tax_id",
            "amount",
            "unit_price",
            "position_total",
        ):
            changed = copy.deepcopy(actual)
            changed["positions"][0][key] = "0"
            with self.subTest(key=key), self.assertRaises(ValueError):
                si._verify(expected, changed)

    def test_final_result_write_failure_warns_draft_exists(self):
        record = si.record_result

        def write(line):
            if "read-back pending" not in line:
                raise OSError("invented disk failure")
            record(line)

        with mock.patch.object(si, "record_result", side_effect=write):
            with self.assertRaisesRegex(
                SystemExit, "draft id 701 exists.*do not run again"
            ):
                self.run_invoice()
        self.assertIn("read-back pending", self.result())
        self.assertEqual(self.calls[-1][0], "GET")
