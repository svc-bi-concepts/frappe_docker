"""Offline tests for the Wise launcher: all HTTP requests are faked."""

import contextlib
import datetime
import io
import json
import os
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

path = os.path.join(os.path.dirname(__file__), "prepare_wise_transfer.py")
with open(path) as source_file:
    lines = source_file.read().splitlines()
start = next(
    i for i, line in enumerate(lines) if line.startswith("exec ") and "<<" in line
)
tag = lines[start].split("<<", 1)[1].strip().strip("'")
end = lines.index(tag, start + 1)
wise = types.ModuleType("prepare_wise_transfer")
wise.__file__ = path
exec(compile("\n".join(lines[start + 1 : end]) + "\n", path, "exec"), wise.__dict__)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class WiseTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        folder = tempfile.TemporaryDirectory(prefix="wise-test-")
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.responses = {}
        wise.PRIVATE = self.root
        self.args = [
            "--recipient-id",
            "42",
            "--invoice",
            "INV-TEST-1",
            "--amount",
            "1081.00",
            "--target-currency",
            "EUR",
            "--source-currency",
            "CHF",
            "--planned-date",
            (datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
        ]
        self.patches = [
            mock.patch.dict(
                os.environ,
                {"FINANCE_WISE_TRANSFER_TOKEN": "invented-token"},
                clear=True,
            ),
            mock.patch.object(wise.OPENER, "open", side_effect=self.open),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def open(self, req, timeout=None):
        path = req.full_url.removeprefix(wise.BASE_URL)
        self.calls.append(
            (req.method, path, json.loads(req.data) if req.data else None, req)
        )
        if path.endswith("/profiles"):
            result = [{"id": 7, "type": "BUSINESS"}]
        elif "/accounts/" in path:
            result = {
                "id": int(path.rsplit("/", 1)[1]),
                "profileId": 7,
                "active": True,
                "currency": "EUR",
                "name": {"fullName": "Example Ltd"},
                "longAccountSummary": "••1234",
            }
        elif path.startswith("/2026Q4/transfers?"):
            result = [
                {
                    "id": 9,
                    "targetAccount": 42,
                    "status": "outgoing_payment_sent",
                    "sourceCurrency": "CHF",
                    "created": "2026-10-01T00:00:00Z",
                }
            ]
        elif path.endswith("/quotes"):
            result = {
                "id": "quote-1",
                "targetAmountAllowed": True,
                "targetAmount": 1081,
                "sourceCurrency": "CHF",
                "targetCurrency": "EUR",
                "sourceAmount": 1100,
                "rateExpirationTime": "2026-11-01T00:00:00Z",
                "paymentOptions": [
                    {"payIn": "BALANCE", "disabled": False, "fee": {"total": 1083}}
                ],
            }
        elif path.endswith("/transfer-requirements"):
            result = []
        elif req.method == "POST" and path.endswith("/transfers"):
            result = {"id": "123456"}
        elif path.endswith("/transfers/123456"):
            result = {
                "id": 123456,
                "status": "incoming_payment_waiting",
                "targetAccount": 42,
                "targetCurrency": "EUR",
                "targetValue": 1081,
                "details": {"reference": "INV-TEST-1"},
                "rateExpirationTime": "2026-11-01T00:00:00Z",
                "created": "2026-10-31T00:00:00Z",
            }
        else:
            raise AssertionError(path)
        replacement = self.responses.get((req.method, path.split("?")[0]), result)
        if callable(replacement):
            replacement = replacement(result, req)
        if isinstance(replacement, Exception):
            raise replacement
        return Response(json.dumps(replacement).encode())

    def run_main(self, args=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wise.main(self.args if args is None else args)
        return out.getvalue()

    def test_first_line_token_and_request_hosts(self):
        with open(wise.__file__) as f:
            self.assertEqual(
                f.readline().rstrip(),
                "#!/usr/local/bin/av inject +FINANCE_WISE_TRANSFER_TOKEN /bin/bash",
            )
        output = self.run_main()
        self.assertIn("recipient: Example Ltd — ••1234", output)
        self.assertTrue(
            all(
                call[3].full_url.startswith("https://api.wise.com/")
                for call in self.calls
            )
        )

    def test_find_recipient_pages_paid_ids_and_matches_normalized_names(self):
        first = [
            {"targetAccount": 9, "status": "outgoing_payment_sent"},
            {"targetAccount": 9, "status": "outgoing_payment_sent"},
            {"targetAccount": 8, "status": "cancelled"},
            {"targetAccount": "bad", "status": "outgoing_payment_sent"},
        ] + [{"targetAccount": None, "status": "cancelled"}] * 96
        second = [{"targetAccount": 11, "status": "outgoing_payment_sent"}]
        self.responses[("GET", "/2026Q4/transfers")] = lambda _row, req: (
            first if "offset=0" in req.full_url else second
        )
        self.responses[("GET", "/2026Q4/accounts/9")] = {
            "id": 9,
            "profileId": 7,
            "active": False,
            "currency": "EUR",
            "name": "  Acme    Example  Ltd ",
            "longAccountSummary": "account ending 9",
        }
        self.responses[("GET", "/2026Q4/accounts/11")] = {
            "id": 11,
            "profileId": 8,
            "active": True,
            "currency": "CHF",
            "name": {"fullName": "Acme Example Ltd"},
            "longAccountSummary": "account ending 11",
        }
        output = self.run_main(["--find-recipient", "eXAMPLE   ltd"])
        self.assertIn(
            "id: 9; name:   Acme    Example  Ltd ; currency: EUR; active: no", output
        )
        self.assertIn("completed transfers: 2", output)
        self.assertIn("paid recipients checked: 2, unreadable: 0", output)
        self.assertFalse(
            any(path.endswith("/accounts/8") for _, path, *_ in self.calls)
        )
        account_reads = [
            path
            for method, path, *_ in self.calls
            if method == "GET" and "/accounts/" in path
        ]
        self.assertEqual(account_reads, ["/2026Q4/accounts/9", "/2026Q4/accounts/11"])
        self.assertTrue(all(method == "GET" for method, *_ in self.calls))
        self.assertEqual(
            [path.split("?")[0] for method, path, *_ in self.calls if method == "GET"][
                1:
            ],
            ["/2026Q4/transfers", "/2026Q4/transfers", *account_reads],
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "wise", "last-result.txt"))
        )
        self.assertNotIn("invented-token", output)

    def test_find_recipient_continues_after_unreadable_account_and_empty_result(self):
        self.responses[("GET", "/2026Q4/transfers")] = [
            {"targetAccount": 3, "status": "outgoing_payment_sent"},
            {"targetAccount": 5, "status": "outgoing_payment_sent"},
        ]
        self.responses[("GET", "/2026Q4/accounts/3")] = urllib.error.HTTPError(
            "https://api.wise.com/2026Q4/accounts/3",
            404,
            "missing",
            {},
            io.BytesIO(b"{}"),
        )
        self.responses[("GET", "/2026Q4/accounts/5")] = {
            "id": 5,
            "profileId": 7,
            "name": "Other Person",
            "active": True,
        }
        output = self.run_main(["--find-recipient", "absent"])
        self.assertIn("no paid recipient matches", output)
        self.assertIn("paid recipients checked: 2, unreadable: 1", output)
        self.assertIn("unreadable recipients were not matched", output)
        self.assertTrue(any(path.endswith("/accounts/5") for _, path, *_ in self.calls))

    def test_find_recipient_rejects_mixed_and_empty_arguments_without_requests(self):
        for extra in (
            self.args,
            ["--yes"],
            ["--detail", "sourceOfFunds=salary"],
            ["--source-currency", "CHF"],
            ["--attempt", "1"],
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                wise.main(["--find-recipient", "Example"] + extra)
            self.assertEqual(self.calls, [])
        with self.assertRaises(SystemExit):
            wise.main(["--find-recipient", "  "])
        self.assertEqual(self.calls, [])

    def test_transfer_mode_requires_each_core_argument_without_requests(self):
        for option in (
            "--recipient-id",
            "--invoice",
            "--amount",
            "--target-currency",
            "--planned-date",
        ):
            args = list(self.args)
            index = args.index(option)
            del args[index : index + 2]
            with self.subTest(option=option), self.assertRaisesRegex(
                SystemExit, option
            ):
                wise.main(args)
            self.assertEqual(self.calls, [])

    def test_dry_run_is_get_only_and_prints_rules(self):
        output = self.run_main()
        self.assertTrue(all(method == "GET" for method, *_ in self.calls))
        self.assertIn("dry run: nothing sent", output)
        self.assertIn("price: known only after the quote, on --yes", output)

    def test_reviewed_set_refuses_unreviewed_paths(self):
        for method, path in (
            ("POST", "/2026Q4/transfers/1/fund"),
            ("POST", "/2026Q4/payments"),
            ("DELETE", "/2026Q4/transfers/1"),
            ("PUT", "/2026Q4/transfers/1/cancel"),
            ("GET", "/2026Q4/accounts/no-id"),
            ("GET", "/2026Q4/accounts/١"),
            ("GET", "/2026Q4/transfers/1?x=y"),
            ("POST", "/2026Q4/accounts"),
            ("POST", "/2026Q4/batch-groups"),
            ("PATCH", "/2026Q4/profiles/7/quotes/1"),
            ("GET", "/2025Q4/profiles"),
        ):
            before = len(self.calls)
            with self.assertRaises(wise.RefusedRequest):
                wise._send(method, path, "invented-token")
            self.assertEqual(len(self.calls), before)

    def test_source_scan_excludes_forbidden_write_paths(self):
        with open(wise.__file__) as f:
            source = f.read().casefold()
        for forbidden in ("/fund", "/payments", "/cancel", "batch-groups"):
            self.assertNotIn(forbidden, source)

    def test_yes_orders_quote_requirements_create_readback(self):
        args = self.args + ["--yes"]
        self.run_main(args)
        writes = [
            (method, path.split("?")[0])
            for method, path, *_ in self.calls
            if method == "POST"
        ]
        self.assertEqual(
            writes,
            [
                ("POST", "/2026Q4/profiles/7/quotes"),
                ("POST", "/2026Q4/transfer-requirements"),
                ("POST", "/2026Q4/transfers"),
            ],
        )

    def test_attempt_changes_idempotency_key(self):
        self.assertEqual(
            wise.transaction_id(42, "INV-TEST-1", 1),
            wise.transaction_id(42, "INV-TEST-1", 1),
        )
        self.assertNotEqual(
            wise.transaction_id(42, "INV-TEST-1", 1),
            wise.transaction_id(42, "INV-TEST-1", 2),
        )

    def test_amount_validation(self):
        for value in ("0", "-1", "1.001", "nan"):
            with self.assertRaises(SystemExit):
                wise.amount_value(value)

    def writes(self):
        return [path for method, path, *_ in self.calls if method == "POST"]

    def change(self, method, path, **values):
        self.responses[(method, "/2026Q4/" + path)] = lambda row, req: dict(
            row, **values
        )

    def refuse(self, message, args=None):
        with self.assertRaisesRegex(SystemExit, message):
            self.run_main(self.args + ["--yes"] if args is None else args)

    def test_token_required_before_any_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.refuse("FINANCE_WISE_TRANSFER_TOKEN")
        self.assertEqual(self.calls, [])

    def test_business_profile_count(self):
        for profiles in (
            [],
            [{"id": 7, "type": "PERSONAL"}],
            [{"id": 7, "type": "BUSINESS"}, {"id": 8, "type": "BUSINESS"}],
        ):
            with self.subTest(profiles=profiles):
                self.responses[("GET", "/2026Q4/profiles")] = profiles
                self.refuse("exactly one BUSINESS")
                self.assertFalse(self.writes())

    def test_recipient_refusals(self):
        for values in ({"profileId": 8}, {"active": False}, {"currency": "USD"}):
            with self.subTest(values=values):
                self.change("GET", "accounts/42", **values)
                self.refuse("Benchi must add or change recipients")
                self.assertFalse(self.writes())

    def test_history_required_for_this_recipient(self):
        for rows in (
            [],
            [{"targetAccount": 43, "status": "outgoing_payment_sent"}],
            [{"targetAccount": 42, "status": "incoming_payment_waiting"}],
        ):
            with self.subTest(rows=rows):
                self.responses[("GET", "/2026Q4/transfers")] = rows
                self.refuse("no completed transfer")
                self.assertFalse(self.writes())

    def test_duplicate_statuses_and_both_reference_locations(self):
        for status in (
            "incoming_payment_waiting",
            "outgoing_payment_sent",
            "bounced_back",
            "unknown",
        ):
            for field in ("reference", "details"):
                with self.subTest(status=status, field=field):
                    row = {"id": 88, "targetAccount": 42, "status": status}
                    row[field] = (
                        " inv-test-1 "
                        if field == "reference"
                        else {"reference": " inv-test-1 "}
                    )
                    self.responses[("GET", "/2026Q4/transfers")] = (
                        lambda rows, req: rows + [row]
                    )
                    self.refuse("88.*" + status)
                    self.assertFalse(self.writes())

    def test_final_unpaid_duplicates_allowed_and_other_recipient_ignored(self):
        for status in ("cancelled", "funds_refunded", "charged_back"):
            with self.subTest(status=status):
                self.responses[("GET", "/2026Q4/transfers")] = (
                    lambda rows, req: rows
                    + [
                        {
                            "id": 88,
                            "targetAccount": 42,
                            "status": status,
                            "reference": "INV-TEST-1",
                        },
                        {
                            "id": 89,
                            "targetAccount": 43,
                            "status": "unknown",
                            "reference": "INV-TEST-1",
                        },
                    ]
                )
                self.assertIn("dry run", self.run_main())

    def test_pagination_and_newest_completed_default_source(self):
        def pages(rows, req):
            if "offset=0" in req.full_url:
                return rows + [{"targetAccount": 43, "status": "unknown"}] * 99
            self.assertIn("offset=100", req.full_url)
            return [
                {
                    "targetAccount": 42,
                    "status": "outgoing_payment_sent",
                    "created": "2026-10-02T00:00:00Z",
                    "sourceCurrency": "USD",
                }
            ]

        self.responses[("GET", "/2026Q4/transfers")] = pages
        args = list(self.args)
        pos = args.index("--source-currency")
        del args[pos : pos + 2]
        self.assertIn('"sourceCurrency": "USD"', self.run_main(args))
        self.assertEqual(len([c for c in self.calls if "transfers?" in c[1]]), 2)
        self.assertTrue(
            all("limit=100" in c[1] for c in self.calls if "transfers?" in c[1])
        )

    def test_duplicate_on_later_page_blocks_create(self):
        def pages(rows, request):
            if "offset=0" in request.full_url:
                return rows + [{"targetAccount": 43, "status": "unknown"}] * 99
            return [
                {
                    "id": 88,
                    "targetAccount": 42,
                    "status": "incoming_payment_waiting",
                    "details": {"reference": "INV-TEST-1"},
                }
            ]

        self.responses[("GET", "/2026Q4/transfers")] = pages
        self.refuse("88.*incoming_payment_waiting")
        self.assertFalse(self.writes())

    def test_date_window_refuses_only_writes(self):
        for days, message in ((-1, "in the past"), (15, "more than 14 days")):
            args = list(self.args)
            args[-1] = (
                datetime.date.today() + datetime.timedelta(days=days)
            ).isoformat()
            self.assertIn("dry run", self.run_main(args))
            self.refuse(message, args + ["--yes"])
            self.assertFalse(self.writes())
        for days in (0, 14):
            args[-1] = (
                datetime.date.today() + datetime.timedelta(days=days)
            ).isoformat()
            self.assertIn("funding: Benchi", self.run_main(args + ["--yes"]))

    def test_quote_mismatches_stop_before_requirements(self):
        for values in (
            {"targetAmountAllowed": False},
            {"targetAmount": 0},
            {"targetAmount": None},
            {"sourceCurrency": "USD"},
            {"targetCurrency": "USD"},
            {"id": None},
        ):
            with self.subTest(values=values):
                self.calls.clear()
                self.change("POST", "profiles/7/quotes", **values)
                self.refuse("refused: quote")
                self.assertEqual(self.writes(), ["/2026Q4/profiles/7/quotes"])

    def test_disabled_balance_reports_reason_and_continues(self):
        self.change(
            "POST",
            "profiles/7/quotes",
            paymentOptions=[
                {
                    "payIn": "BALANCE",
                    "disabled": True,
                    "disabledReason": {"message": "Example reason"},
                    "fee": {"total": 1083},
                }
            ],
        )
        output = self.run_main(self.args + ["--yes"])
        self.assertIn("BALANCE option disabled: Example reason", output)
        self.assertIn("funding: Benchi", output)

    def test_requirements_get_exact_planned_create_body(self):
        self.run_main(self.args + ["--yes", "--detail", "sourceOfFunds=example"])
        bodies = [c[2] for c in self.calls if c[0] == "POST"]
        self.assertEqual(bodies[1], bodies[2])
        self.assertEqual(bodies[1]["quoteUuid"], "quote-1")
        self.assertEqual(bodies[1]["details"]["sourceOfFunds"], "example")

    def test_nested_requirements_and_reference_constraints(self):
        for item, message in (
            ({"key": "sourceOfFunds", "required": True}, "sourceOfFunds.*--detail"),
            ({"key": "reference", "maxLength": 2}, "maxLength"),
            ({"key": "reference", "minLength": 100}, "minLength"),
            ({"key": "reference", "validationRegexp": "[0-9]+"}, "validationRegexp"),
        ):
            with self.subTest(item=item):
                self.calls.clear()
                self.responses[("POST", "/2026Q4/transfer-requirements")] = [
                    {
                        "type": "transfer",
                        "fields": [{"name": "Example field", "group": [item]}],
                    }
                ]
                self.refuse(message)
                self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.args[self.args.index("--invoice") + 1], "INV-TEST-1")

    def test_supplied_required_detail_allows_create(self):
        self.responses[("POST", "/2026Q4/transfer-requirements")] = [
            {"fields": [{"group": [{"key": "sourceOfFunds", "required": True}]}]}
        ]
        self.refuse(
            "sourceOfFunds.*--detail",
            self.args + ["--yes", "--detail", "sourceOfFunds= "],
        )
        self.assertIn(
            "funding: Benchi",
            self.run_main(self.args + ["--yes", "--detail", "sourceOfFunds=example"]),
        )

    def test_unsupported_details_and_attempt_refused(self):
        for extra in (
            ["--detail", "reference=other"],
            ["--detail", "bad"],
            ["--attempt", "0"],
        ):
            self.refuse("refused", self.args + extra)
            self.assertFalse(self.calls)

    def test_readback_mismatches_print_id_and_record_failure(self):
        for values in (
            {"status": "unknown"},
            {"targetAccount": 43},
            {"details": {"reference": "other"}},
            {"targetCurrency": "USD"},
            {"targetValue": 0},
            {"targetValue": None},
            {"reference": "INV-TEST-1", "details": {"reference": "other"}},
            {"targetValue": "sNaN"},
        ):
            with self.subTest(values=values):
                self.calls.clear()
                self.change("GET", "transfers/123456", **values)
                self.refuse("123456 read-back mismatch")
                self.assertEqual(len(self.writes()), 3)
        with open(os.path.join(self.root, "wise", "last-result.txt")) as f:
            self.assertIn("transfer_id: 123456", f.read())

    def test_http_errors_never_retry_or_leak_body_or_token(self):
        for endpoint in ("profiles/7/quotes", "transfer-requirements", "transfers"):
            with self.subTest(endpoint=endpoint):
                self.calls.clear()
                self.responses.clear()
                self.responses[("POST", "/2026Q4/" + endpoint)] = (
                    urllib.error.HTTPError(
                        "https://api.wise.com/" + endpoint,
                        400,
                        "invented-token",
                        {},
                        io.BytesIO(
                            json.dumps(
                                {
                                    "errors": [
                                        {"code": "EXAMPLE", "message": "invented-token"}
                                    ]
                                }
                            ).encode()
                        ),
                    )
                )
                self.refuse("HTTP 400.*EXAMPLE")
                self.assertEqual(self.writes().count("/2026Q4/" + endpoint), 1)
                self.assertEqual(self.writes()[-1], "/2026Q4/" + endpoint)

    def test_approval_error_keeps_status_path_and_fixed_guidance(self):
        self.responses[("POST", "/2026Q4/transfers")] = urllib.error.HTTPError(
            "https://api.wise.com/2026Q4/transfers",
            403,
            "example",
            {},
            io.BytesIO(b'{"message":"missing approval invented-token"}'),
        )
        self.refuse("transfers.*HTTP 403.*payment approval rule.*Benchi")

    def test_lost_connections_and_invalid_json_stop_each_post(self):
        for endpoint in ("profiles/7/quotes", "transfer-requirements", "transfers"):
            for error in (
                urllib.error.URLError("invented-token"),
                TimeoutError("invented-token"),
                ConnectionResetError("invented-token"),
                ValueError("invented-token"),
            ):
                with self.subTest(endpoint=endpoint, error=type(error)):
                    self.calls.clear()
                    self.responses.clear()
                    self.responses[("POST", "/2026Q4/" + endpoint)] = error
                    self.refuse("no usable answer")
                    self.assertEqual(self.writes()[-1], "/2026Q4/" + endpoint)
                    self.assertEqual(self.writes().count("/2026Q4/" + endpoint), 1)

    def test_readback_connection_failure_preserves_transfer_id(self):
        self.responses[("GET", "/2026Q4/transfers/123456")] = TimeoutError(
            "invented-token"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            wise.main(self.args + ["--yes"])
        self.assertIn("transfer id: 123456", output.getvalue())
        with open(os.path.join(self.root, "wise", "last-result.txt")) as f:
            self.assertIn("read-back failed; transfer_id: 123456", f.read())

    def test_token_absent_from_output_errors_and_result(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            wise.main(self.args + ["--yes"])
        self.assertNotIn("invented-token", out.getvalue() + err.getvalue())
        for method, path, body, request in self.calls:
            self.assertEqual(
                request.get_header("Authorization"), "Bearer invented-token"
            )
            self.assertNotIn("invented-token", path + json.dumps(body))
        with open(os.path.join(self.root, "wise", "last-result.txt")) as f:
            text = f.read()
        for forbidden in ("invented-token", "INV-TEST-1", "1081", "Example Ltd"):
            self.assertNotIn(forbidden, text)
        self.responses[("POST", "/2026Q4/profiles/7/quotes")] = urllib.error.HTTPError(
            "https://api.wise.com/2026Q4/profiles/7/quotes",
            400,
            "invented-token",
            {},
            io.BytesIO(b'{"errors":[{"code":"invented-token"}]}'),
        )
        with self.assertRaises(SystemExit) as raised:
            self.run_main(self.args + ["--yes"])
        self.assertNotIn("invented-token", str(raised.exception))

    def test_redirects_do_not_forward_authorization(self):
        calls = []

        class RedirectTransport(wise.urllib.request.HTTPSHandler):
            def https_open(self, request):
                calls.append(request.full_url)
                response = wise.urllib.request.addinfourl(
                    io.BytesIO(b""),
                    {"location": "https://example.invalid/"},
                    request.full_url,
                    302,
                )
                response.msg = "Found"
                return response

        opener = wise.urllib.request.build_opener(
            wise.NoRedirect(), RedirectTransport()
        )
        with mock.patch.object(wise, "OPENER", opener):
            with self.assertRaisesRegex(SystemExit, "HTTP 302"):
                wise._send("GET", "/2026Q4/profiles", "invented-token")
        self.assertEqual(calls, ["https://api.wise.com/2026Q4/profiles"])

    def test_plain_text_approval_error_has_fixed_guidance(self):
        self.responses[("POST", "/2026Q4/transfers")] = urllib.error.HTTPError(
            "https://api.wise.com/2026Q4/transfers",
            403,
            "example",
            {},
            io.BytesIO(b"missing approval invented-token"),
        )
        self.refuse("transfers.*HTTP 403.*payment approval rule.*Benchi")

    def test_unusable_requirements_stop_before_create(self):
        self.responses[("POST", "/2026Q4/transfer-requirements")] = {}
        self.refuse("requirements without a field list")
        self.assertEqual(len(self.writes()), 2)

    def test_success_summary_and_minimal_result(self):
        output = self.run_main(self.args + ["--yes"])
        for expected in (
            "status: incoming_payment_waiting",
            "source: 1100 CHF",
            "BALANCE fee: 1083",
            "target: 1081.00 EUR",
            "rate held until 2026-11-01T00:00:00Z, then it floats: check the amount when funding",
            "Wise cancels this unfunded transfer about 2026-11-14",
            "funding: Benchi, in the Wise web UI",
        ):
            self.assertIn(expected, output)
        with open(os.path.join(self.root, "wise", "last-result.txt")) as f:
            self.assertEqual(
                f.read(),
                "outcome: prepared; transfer_id: 123456; status: incoming_payment_waiting\n",
            )

    def test_extreme_amounts_refused_without_decimal_traceback(self):
        for value in ("1e999", "9" * 40 + ".01", "sNaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                wise.amount_value(value)


if __name__ == "__main__":
    unittest.main()
