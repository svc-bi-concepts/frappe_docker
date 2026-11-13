"""Offline boundary tests for the draft invoice from an existing bexio order."""

import base64
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
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(os.path.abspath(__file__))
oi = launcher.load("create_invoice_from_order.py")
from private_guard import setUpModule  # noqa: E402

# The order action's body and the invoice edit's writable fields, as the published
# docs give them (v2CreateInvoiceFromOrder, v2EditInvoice); not proven live.
ORDER_ACTION_FIELDS = {"positions"}
ORDER_ACTION_POSITION = {"id", "type", "amount"}
EDIT_FIELDS = {
    "title",
    "contact_id",
    "contact_sub_id",
    "user_id",
    "pr_project_id",
    "language_id",
    "bank_account_id",
    "currency_id",
    "payment_type_id",
    "header",
    "footer",
    "mwst_type",
    "mwst_is_net",
    "show_position_taxes",
    "is_valid_from",
    "is_valid_to",
    "contact_address_manual",
    "reference",
    "api_reference",
    "template_slug",
}
EDIT_REQUIRED = {"contact_id", "user_id", "is_valid_from", "is_valid_to"}


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def http_error(req, code):
    return urllib.error.HTTPError(
        req.full_url,
        code,
        "invented refusal",
        {},
        io.BytesIO(b'{"message": "invented refusal detail"}'),
    )


def day_position(ident=11, amount="60.000000", open_amount="60.000000"):
    return {
        "id": ident,
        "type": "KbPositionCustom",
        "text": "<p>Invented consulting days</p>",
        "amount": amount,
        "amount_open": open_amount,
        "unit_id": 5,
        "unit_name": "days",
        "unit_price": "913.370000",
        "account_id": 30,
        "tax_id": 8,
    }


def order(positions=None):
    return {
        "id": 300,
        "document_nr": "INVENTED-ORDER",
        "title": "Invented framework order",
        "contact_id": 42,
        "user_id": 9,
        "mwst_type": 0,
        "mwst_is_net": True,
        "total_net": "invented order net",
        "total_taxes": "invented order taxes",
        "total": "invented order total",
        "positions": positions
        or [
            {"id": 10, "type": "KbPositionText", "text": "Invented intro"},
            day_position(),
        ],
    }


def fake_bexio(the_order=None):
    """bexio as its docs describe it: a 422 for any field it does not take."""
    calls, invoices = [], {}
    the_order = order() if the_order is None else the_order
    responses = {
        "/2.0/kb_order/300": the_order,
        "/2.0/contact/42": {"id": 42, "name_1": "Invented Customer"},
        "/3.0/taxes": [{"id": 8, "code": "VAT81", "value": "8.1"}],
        "/2.0/accounts": [{"id": 30, "account_no": "3400"}],
        "/2.0/kb_invoice": [],
    }

    def urlopen(req, timeout=None):
        parsed = urllib.parse.urlparse(req.full_url)
        method, path = req.get_method(), parsed.path
        calls.append((method, path, req))
        if method == "POST" and path == "/2.0/kb_order/300/invoice":
            body = json.loads(req.data.decode("utf-8"))
            by_id = {p["id"]: p for p in the_order["positions"]}
            if set(body) - ORDER_ACTION_FIELDS:
                raise http_error(req, 422)
            sent = body.get("positions", [])
            for row in sent:
                if (
                    set(row) - ORDER_ACTION_POSITION
                    or {"id", "type"} - set(row)
                    or row["id"] not in by_id
                    or by_id[row["id"]]["type"] != row["type"]
                    or ("amount" in row and type(row["amount"]) is not int)
                ):
                    raise http_error(req, 422)
            rows = []
            for row in sent:
                copied = copy.deepcopy(by_id[row["id"]])
                if "amount" in row:
                    copied["amount"] = "{}.000000".format(row["amount"])
                rows.append(copied)
            invoice = {
                "id": 701,
                "document_nr": "INVENTED-DRAFT",
                "kb_item_status_id": 7,
                "title": the_order["title"],
                "contact_id": the_order["contact_id"],
                "contact_sub_id": None,
                "user_id": 9,
                "project_id": None,
                "language_id": 1,
                "bank_account_id": 1,
                "currency_id": 1,
                "payment_type_id": 1,
                "header": "Invented header",
                "footer": "Invented footer",
                "mwst_type": 0,
                "mwst_is_net": True,
                "show_position_taxes": False,
                "template_slug": None,
                "reference": None,
                "is_valid_from": "2026-03-04",
                "is_valid_to": "2026-04-03",
                "total_net": "invented net",
                "total_taxes": "invented taxes",
                "total": "invented total",
                "positions": rows,
            }
            invoices[701] = invoice
            return _Response(invoice)
        if method == "POST" and path == "/2.0/kb_invoice/701":
            body = json.loads(req.data.decode("utf-8"))
            if set(body) - EDIT_FIELDS or EDIT_REQUIRED - set(body):
                raise http_error(req, 422)
            invoices[701].update(body)
            return _Response(invoices[701])
        if method == "GET" and path == "/2.0/kb_invoice/701":
            return _Response(invoices[701])
        if method != "GET":
            raise AssertionError(f"unexpected {method} {path}")
        value = copy.deepcopy(responses[path])
        if isinstance(value, list):
            query = urllib.parse.parse_qs(parsed.query)
            offset = int(query.get("offset", [0])[0])
            limit = int(query.get("limit", [500])[0])
            value = value[offset : offset + limit]
        return _Response(value)

    urlopen.responses = responses
    urlopen.invoices = invoices
    return urlopen, calls


class LauncherTest(unittest.TestCase):
    PATH = os.path.join(
        os.path.dirname(os.path.abspath(oi.__file__)), "create_invoice_from_order.py"
    )

    def test_head_and_mode(self):
        with open(self.PATH, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(
            content.splitlines()[0], "#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash"
        )
        self.assertTrue(os.stat(self.PATH).st_mode & stat.S_IXUSR)


def reset_writes():
    oi._sent_order_invoice.clear()
    oi._created.clear()
    oi._deletable.clear()


class RequestBoundaryTest(unittest.TestCase):
    def setUp(self):
        reset_writes()
        self.addCleanup(reset_writes)

    def test_only_order_action_and_own_draft_edit_are_writes(self):
        for method, path in (
            ("POST", "/2.0/kb_invoice"),
            ("POST", "/2.0/kb_invoice/1"),
            ("PUT", "/2.0/kb_invoice/1"),
            ("DELETE", "/2.0/kb_invoice/1"),
            ("POST", "/2.0/kb_invoice/1/issue"),
            ("POST", "/2.0/kb_invoice/1/send"),
            ("POST", "/2.0/kb_invoice/1/mark_as_sent"),
            ("POST", "/2.0/kb_order/1"),
            ("DELETE", "/2.0/kb_order/1"),
            ("POST", "/2.0/kb_order/1/delivery"),
            ("POST", "/2.0/kb_order/1/repetition"),
            ("POST", "/2.0/kb_invoice/1/kb_position_custom/2"),
            ("GET", "/2.0/kb_order/nope"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(
                oi.RefusedRequest
            ):
                oi._send(method, path, "invented")

    def test_delete_only_of_checked_draft_and_only_once(self):
        self.assertFalse(oi._allowed("DELETE", "/2.0/kb_invoice/905"))
        oi._deletable.add(905)
        self.assertTrue(oi._allowed("DELETE", "/2.0/kb_invoice/905"))
        self.assertFalse(oi._allowed("DELETE", "/2.0/kb_invoice/906"))
        self.assertFalse(oi._allowed("DELETE", "/2.0/kb_order/905"))
        self.assertFalse(oi._allowed("POST", "/2.0/kb_invoice/905"))
        self.assertTrue(oi._allowed("GET", "/2.0/kb_invoice/905/pdf"))
        self.assertFalse(oi._allowed("POST", "/2.0/kb_invoice/905/pdf"))
        with mock.patch.object(
            oi.urllib.request, "urlopen", return_value=_Response({"success": True})
        ):
            oi._send("DELETE", "/2.0/kb_invoice/905", "invented")
        with self.assertRaises(oi.RefusedRequest):
            oi._send("DELETE", "/2.0/kb_invoice/905", "invented")

    def test_order_action_once_and_edit_only_of_created_draft(self):
        self.assertTrue(oi._allowed("POST", "/2.0/kb_order/300/invoice"))
        oi._sent_order_invoice.append("/2.0/kb_order/300/invoice")
        self.assertFalse(oi._allowed("POST", "/2.0/kb_order/300/invoice"))
        self.assertFalse(oi._allowed("POST", "/2.0/kb_invoice/701"))
        oi._created.add(701)
        self.assertTrue(oi._allowed("POST", "/2.0/kb_invoice/701"))
        self.assertFalse(oi._allowed("POST", "/2.0/kb_invoice/702"))


class RunTest(unittest.TestCase):
    def setUp(self):
        reset_writes()
        self.addCleanup(reset_writes)
        self.temp = tempfile.TemporaryDirectory(prefix="order-invoice-test-")
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(oi, "PRIVATE", self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake, self.calls = fake_bexio()
        self.output = io.StringIO()

    def run_tool(self, *extra, yes=True, quantity="20", date="2026-03-31"):
        args = ["--order", "300", "--contact", "42", "--date", date]
        if quantity is not None:
            args += ["--quantity", quantity]
        args += list(extra) + (["--yes"] if yes else [])
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            oi.urllib.request, "urlopen", side_effect=self.fake
        ), contextlib.redirect_stdout(
            self.output
        ):
            oi.main(args)

    def posts(self):
        return [(path, req) for method, path, req in self.calls if method != "GET"]

    def result(self):
        path = os.path.join(self.temp.name, oi.RESULT)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as source:
            return source.read()

    def test_no_token_stops_before_network(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            oi.urllib.request, "urlopen"
        ) as send:
            with self.assertRaises(SystemExit) as error:
                oi.main(["--order", "300", "--contact", "42", "--date", "2026-03-31"])
        self.assertIn("BEXIO_TOKEN", str(error.exception))
        send.assert_not_called()

    def test_bad_arguments_are_refused_before_network(self):
        for args in (
            ["--contact", "42", "--date", "2026-03-31"],
            ["--order", "300", "--date", "2026-03-31"],
            ["--order", "300", "--contact", "42"],
            ["--order", "300", "--contact", "42", "--date", "2026-3-31"],
            ["--order", "x", "--contact", "42", "--date", "2026-03-31"],
            [
                "--order",
                "300",
                "--contact",
                "42",
                "--date",
                "2026-03-31",
                "--quantity",
                "0",
            ],
            [
                "--order",
                "300",
                "--contact",
                "42",
                "--date",
                "2026-03-31",
                "--quantity",
                "2.5",
            ],
        ):
            with self.subTest(args=args), mock.patch.object(
                oi.urllib.request, "urlopen"
            ) as send, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    oi.main(args)
                send.assert_not_called()

    def test_dry_run_prints_plan_and_writes_nothing(self):
        self.run_tool(yes=False)
        out = self.output.getvalue()
        self.assertFalse(self.posts())
        self.assertEqual(os.listdir(self.temp.name), [])
        self.assertIn("order 300 (INVENTED-ORDER)", out)
        self.assertIn("Invented Customer", out)
        self.assertIn("position 11 KbPositionCustom: Invented consulting days", out)
        self.assertIn("account 3400; tax VAT81", out)
        self.assertIn("quantity 20 of 60.000000", out)
        self.assertIn("net 18267.40, tax 1479.66", out)
        self.assertIn("dry run: nothing created", out)
        tax_req = next(req for _, path, req in self.calls if path == "/3.0/taxes")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(tax_req.full_url).query)
        self.assertEqual(query["date"], ["2026-03-31"])

    def test_estimate_follows_the_order_vat_mode(self):
        for mwst_type, is_net, expected in (
            (0, False, "net 16898.61, tax 1368.79"),
            (2, True, "net 18267.40, tax 0.00"),
            (1, True, "estimate: none for mwst_type 1"),
        ):
            with self.subTest(mwst_type=mwst_type, is_net=is_net):
                the_order = order()
                the_order.update(mwst_type=mwst_type, mwst_is_net=is_net)
                self.fake, self.calls = fake_bexio(the_order)
                self.output = io.StringIO()
                self.run_tool(yes=False)
                self.assertIn(expected, self.output.getvalue())
                self.assertFalse(self.posts())

    def test_missing_quantity_shows_order_and_refuses(self):
        with self.assertRaises(SystemExit) as error:
            self.run_tool(quantity=None)
        self.assertIn("--quantity is required", str(error.exception))
        self.assertIn("position 11", self.output.getvalue())
        self.assertFalse(self.posts())

    def test_yes_creates_draft_from_order_with_quantity_and_date(self):
        self.run_tool()
        posts = self.posts()
        self.assertEqual(
            [path for path, _ in posts],
            ["/2.0/kb_order/300/invoice", "/2.0/kb_invoice/701"],
        )
        body = json.loads(posts[0][1].data)
        self.assertEqual(
            body,
            {
                "positions": [
                    {"id": 10, "type": "KbPositionText"},
                    {"id": 11, "type": "KbPositionCustom", "amount": 20},
                ]
            },
        )
        edit = json.loads(posts[1][1].data)
        self.assertEqual(edit["is_valid_from"], "2026-03-31")
        # The draft's 30-day term from the order is kept.
        self.assertEqual(edit["is_valid_to"], "2026-04-30")
        self.assertEqual(edit["header"], "Invented header")
        invoice = self.fake.invoices[701]
        self.assertEqual(invoice["kb_item_status_id"], 7)
        out = self.output.getvalue()
        self.assertIn("created draft id 701, document_nr INVENTED-DRAFT", out)
        self.assertIn(
            "from order id 300 (INVENTED-ORDER), position 11 quantity 20", out
        )
        self.assertIn("date 2026-03-31 due 2026-04-30", out)
        self.assertIn("total_net: invented net", out)
        result = self.result()
        self.assertIn(
            "sales invoice id 701 from order id 300 read-back pending", result
        )
        self.assertIn("document_nr INVENTED-DRAFT draft verified", result)
        self.assertNotIn("Invented Customer", result)
        self.assertNotIn("invented-token", result)

    def test_no_edit_when_draft_already_has_the_date(self):
        self.run_tool(date="2026-03-04")
        self.assertEqual([p for p, _ in self.posts()], ["/2.0/kb_order/300/invoice"])

    def test_other_contact_is_refused_before_any_write(self):
        self.fake.responses["/2.0/kb_order/300"]["contact_id"] = 43
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("belongs to contact 43, not 42", str(error.exception))
        self.assertFalse(self.posts())

    def test_two_billable_positions_need_position(self):
        second = day_position(ident=12)
        self.fake, self.calls = fake_bexio(order([day_position(), second]))
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("2 billable positions", str(error.exception))
        self.assertFalse(self.posts())
        reset_writes()
        self.run_tool("--position", "12")
        body = json.loads(self.posts()[0][1].data)
        self.assertEqual(
            body, {"positions": [{"id": 12, "type": "KbPositionCustom", "amount": 20}]}
        )

    def test_unknown_position_and_unhandled_types_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_tool("--position", "10")
        discount = {"id": 13, "type": "KbPositionDiscount", "value": "10"}
        self.fake, self.calls = fake_bexio(order([day_position(), discount]))
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("KbPositionDiscount", str(error.exception))
        self.assertFalse(self.posts())

    def test_quantity_above_open_amount_is_refused(self):
        self.fake, self.calls = fake_bexio(
            order([day_position(open_amount="10.000000")])
        )
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("more than the position's open 10", str(error.exception))
        self.assertFalse(self.posts())

    def test_existing_invoice_same_title_and_date_is_refused(self):
        self.fake.responses["/2.0/kb_invoice"] = [
            {
                "contact_id": 42,
                "title": "Invented framework order",
                "is_valid_from": "2026-03-31",
            }
        ]
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("exists", str(error.exception))
        self.assertFalse(self.posts())

    def test_fake_refuses_missing_or_unknown_fields(self):
        req = urllib.request.Request(
            "https://api.bexio.com/2.0/kb_order/300/invoice",
            data=json.dumps({"positions": [{"id": 11, "amount": 2}]}).encode(),
            method="POST",
        )
        for body in (
            {"positions": [{"id": 11, "amount": 2}]},
            {"positions": [{"id": 11, "type": "KbPositionCustom", "amount": "2"}]},
            {"positions": [{"id": 11, "type": "KbPositionCustom", "price": 1}]},
            {"title": "x"},
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError):
                req.data = json.dumps(body).encode()
                self.fake(req)
        self.fake(
            urllib.request.Request(
                "https://api.bexio.com/2.0/kb_order/300/invoice",
                data=b"{}",
                method="POST",
            )
        )
        edit = urllib.request.Request(
            "https://api.bexio.com/2.0/kb_invoice/701",
            data=json.dumps({"contact_id": 42, "is_valid_from": "2026-03-31"}).encode(),
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError):
            self.fake(edit)

    def test_post_refusal_is_not_retried(self):
        original = self.fake

        def refuse(req, timeout=None):
            if req.get_method() == "POST":
                self.calls.append(
                    ("POST", urllib.parse.urlparse(req.full_url).path, req)
                )
                raise http_error(req, 403)
            return original(req, timeout)

        self.fake = refuse
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("HTTP 403", str(error.exception))
        self.assertIn("kb_invoice_edit", str(error.exception))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.result(), "")
        self.assertEqual(self.calls[-1][0], "POST")

    def test_readback_mismatch_records_existing_draft(self):
        original = self.fake

        def wrong_amount(req, timeout=None):
            answer = original(req, timeout)
            if req.get_method() == "GET" and req.full_url.endswith(
                "/2.0/kb_invoice/701"
            ):
                body = json.loads(answer.body)
                body["positions"][-1]["amount"] = "60.000000"
                return _Response(body)
            return answer

        self.fake = wrong_amount
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("draft id 701 exists; read-back failed", str(error.exception))
        self.assertIn("not the quantity", str(error.exception))
        self.assertIn("read-back failed", self.result())
        # No date edit follows a read-back that does not match.
        self.assertEqual([p for p, _ in self.posts()], ["/2.0/kb_order/300/invoice"])

    def test_issued_status_fails_readback(self):
        original = self.fake

        def issued(req, timeout=None):
            answer = original(req, timeout)
            if req.get_method() == "GET" and req.full_url.endswith(
                "/2.0/kb_invoice/701"
            ):
                body = json.loads(answer.body)
                body["kb_item_status_id"] = 8
                return _Response(body)
            return answer

        self.fake = issued
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("draft status is not 7", str(error.exception))

    def test_date_edit_that_changes_another_field_fails_readback(self):
        original = self.fake

        def drops_header(req, timeout=None):
            answer = original(req, timeout)
            if req.get_method() == "POST" and req.full_url.endswith(
                "/2.0/kb_invoice/701"
            ):
                self.fake_invoices[701]["header"] = ""
            return answer

        self.fake_invoices = original.invoices
        self.fake = drops_header
        with self.assertRaises(SystemExit) as error:
            self.run_tool()
        self.assertIn("header changed by the date edit", str(error.exception))
        self.assertIn("read-back failed", self.result())


INVENTED_PDF = b"%PDF-1.4 invented invoice\n%%EOF\n"


def draft(**changes):
    invoice = {
        "id": 905,
        "document_nr": "INVENTED-00905",
        "kb_item_status_id": 7,
        "title": "Invented framework order",
        "contact_id": 42,
        "is_valid_from": "2026-03-31",
    }
    invoice.update(changes)
    return invoice


def fake_invoices(invoice):
    """bexio for one invoice: read, PDF, list, and a DELETE that removes a draft."""
    calls, listed = [], {invoice["id"]: invoice}

    def urlopen(req, timeout=None):
        parsed = urllib.parse.urlparse(req.full_url)
        method, path = req.get_method(), parsed.path
        calls.append((method, path))
        one = "/2.0/kb_invoice/{}".format(invoice["id"])
        if method == "GET" and path == one:
            return _Response(listed[invoice["id"]])
        if method == "GET" and path == one + "/pdf":
            return _Response(
                {
                    "name": "invented.pdf",
                    "size": len(INVENTED_PDF),
                    "mime": "application/pdf",
                    "content": base64.b64encode(INVENTED_PDF).decode(),
                }
            )
        if method == "GET" and path == "/2.0/kb_invoice":
            return _Response(list(listed.values()))
        if method == "DELETE" and path == one:
            if listed[invoice["id"]]["kb_item_status_id"] != 7:
                raise http_error(req, 422)
            del listed[invoice["id"]]
            return _Response({"success": True})
        raise AssertionError(f"unexpected {method} {path}")

    return urlopen, calls


class SideModeTest(unittest.TestCase):
    def setUp(self):
        reset_writes()
        self.addCleanup(reset_writes)
        self.temp = tempfile.TemporaryDirectory(prefix="order-invoice-test-")
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(oi, "PRIVATE", self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.output = io.StringIO()

    def run_tool(self, fake, *args):
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            oi.urllib.request, "urlopen", side_effect=fake
        ), contextlib.redirect_stdout(
            self.output
        ):
            oi.main(["--contact", "42"] + list(args))

    def result(self):
        with open(os.path.join(self.temp.name, oi.RESULT), encoding="utf-8") as f:
            return f.read()

    def delete_args(self, nr="INVENTED-00905", yes=True):
        args = ["--delete-draft", "905", "--document-nr", nr]
        return args + (["--yes"] if yes else [])

    def test_delete_dry_run_sends_no_delete(self):
        fake, calls = fake_invoices(draft())
        self.run_tool(fake, *self.delete_args(yes=False))
        self.assertEqual(calls, [("GET", "/2.0/kb_invoice/905")])
        self.assertIn("dry run: nothing deleted", self.output.getvalue())
        self.assertEqual(os.listdir(self.temp.name), [])

    def test_delete_removes_matching_draft_and_checks_the_list(self):
        fake, calls = fake_invoices(draft())
        self.run_tool(fake, *self.delete_args())
        self.assertEqual(
            calls,
            [
                ("GET", "/2.0/kb_invoice/905"),
                ("DELETE", "/2.0/kb_invoice/905"),
                ("GET", "/2.0/kb_invoice"),
            ],
        )
        self.assertIn("deleted draft id 905", self.output.getvalue())
        with open(os.path.join(self.temp.name, oi.RESULT), encoding="utf-8") as f:
            self.assertEqual(
                f.read().splitlines(),
                [
                    "sales invoice draft id 905 document_nr INVENTED-00905"
                    " delete sent, list check pending",
                    "sales invoice draft id 905 document_nr INVENTED-00905"
                    " deleted, no longer listed",
                ],
            )
        self.assertFalse(oi._deletable)

    def test_delete_refuses_issued_other_contact_or_other_number(self):
        for invoice, nr, expected in (
            (draft(kb_item_status_id=8), "INVENTED-00905", "status 8, not draft"),
            (draft(contact_id=43), "INVENTED-00905", "contact 43, not 42"),
            (draft(), "INVENTED-00904", "is INVENTED-00905, not INVENTED-00904"),
        ):
            with self.subTest(expected=expected):
                fake, calls = fake_invoices(invoice)
                with self.assertRaises(SystemExit) as error:
                    self.run_tool(fake, *self.delete_args(nr=nr))
                self.assertIn(expected, str(error.exception))
                self.assertEqual([m for m, _ in calls], ["GET"])
                self.assertFalse(oi._deletable)

    def test_delete_still_listed_is_reported(self):
        fake, calls = fake_invoices(draft())

        def keeps(req, timeout=None):
            if req.get_method() == "DELETE":
                calls.append(("DELETE", urllib.parse.urlparse(req.full_url).path))
                return _Response({"success": True})
            return fake(req, timeout)

        with self.assertRaises(SystemExit) as error:
            self.run_tool(keeps, *self.delete_args())
        self.assertIn("still listed", str(error.exception))
        self.assertIn("still listed after the delete", self.result())

    def test_delete_sent_is_recorded_when_the_list_check_fails(self):
        fake, calls = fake_invoices(draft())

        def list_fails(req, timeout=None):
            path = urllib.parse.urlparse(req.full_url).path
            if req.get_method() == "GET" and path == "/2.0/kb_invoice":
                raise http_error(req, 502)
            return fake(req, timeout)

        with self.assertRaises(SystemExit) as error:
            self.run_tool(list_fails, *self.delete_args())
        self.assertIn("delete of draft id 905 sent; list check failed", str(error.exception))
        self.assertIn("delete sent, list check pending", self.result())
        self.assertIn("list check failed", self.result())
        self.assertEqual([m for m, _ in calls], ["GET", "DELETE"])

    def test_pdf_saved_under_private_never_over_a_file(self):
        fake, calls = fake_invoices(draft())
        out = os.path.join(self.temp.name, "customer", "invented-invoice.pdf")
        self.run_tool(fake, "--pdf", "905", "--out", out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), INVENTED_PDF)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(
            calls, [("GET", "/2.0/kb_invoice/905"), ("GET", "/2.0/kb_invoice/905/pdf")]
        )
        self.assertIn("as invented-invoice.pdf", self.output.getvalue())
        with self.assertRaises(SystemExit) as error:
            self.run_tool(fake, "--pdf", "905", "--out", out)
        self.assertIn("never overwrites", str(error.exception))

    def test_pdf_refuses_outside_private_other_contact_or_no_pdf(self):
        fake, calls = fake_invoices(draft())
        with tempfile.TemporaryDirectory() as elsewhere:
            with self.assertRaises(SystemExit) as error:
                self.run_tool(fake, "--pdf", "905", "--out", elsewhere + "/x.pdf")
        self.assertIn("under", str(error.exception))
        self.assertEqual(calls, [])
        fake, calls = fake_invoices(draft(contact_id=43))
        with self.assertRaises(SystemExit):
            self.run_tool(fake, "--pdf", "905", "--out", self.temp.name + "/x.pdf")
        self.assertEqual(calls, [("GET", "/2.0/kb_invoice/905")])
        original, calls = fake_invoices(draft())

        def not_pdf(req, timeout=None):
            if req.full_url.endswith("/pdf"):
                return _Response({"content": base64.b64encode(b"<html>").decode()})
            return original(req, timeout)

        with self.assertRaises(SystemExit) as error:
            self.run_tool(not_pdf, "--pdf", "905", "--out", self.temp.name + "/y.pdf")
        self.assertIn("without a PDF", str(error.exception))
        self.assertFalse(os.path.exists(self.temp.name + "/y.pdf"))

    def test_mode_arguments_are_checked_before_network(self):
        for args in (
            ["--delete-draft", "905"],
            ["--pdf", "905"],
            ["--pdf", "905", "--out", "/x.pdf", "--yes"],
            ["--order", "300"],
            ["--order", "300", "--date", "2026-03-31", "--pdf", "1", "--out", "/x.pdf"],
            ["--delete-draft", "905", "--document-nr", "A", "--quantity", "2"],
            ["--delete-draft", "905", "--document-nr", "A B"],
            ["--order", "300", "--date", "2026-03-31", "--document-nr", "A"],
        ):
            with self.subTest(args=args), mock.patch.object(
                oi.urllib.request, "urlopen"
            ) as send, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    oi.main(["--contact", "42"] + args)
                send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
