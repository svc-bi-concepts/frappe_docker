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

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "bexio"))
path = os.path.join(os.path.dirname(__file__), "prepare_wise_transfer.py")
with open(path) as source_file:
    lines = source_file.read().splitlines()
start = next(i for i, line in enumerate(lines) if line.startswith("exec ") and "<<" in line)
tag = lines[start].split("<<", 1)[1].strip().strip("'")
end = lines.index(tag, start + 1)
wise = types.ModuleType("prepare_wise_transfer")
wise.__file__ = path
exec(compile("\n".join(lines[start + 1:end]) + "\n", path, "exec"), wise.__dict__)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class WiseTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.root = tempfile.mkdtemp(prefix="wise-test-")
        wise.PRIVATE = self.root
        self.args = ["--recipient-id", "42", "--invoice", "INV-TEST-1", "--amount", "1081.00",
                     "--target-currency", "EUR", "--source-currency", "CHF", "--planned-date",
                     (datetime.date.today() + datetime.timedelta(days=1)).isoformat()]
        self.patches = [mock.patch.dict(os.environ, {"FINANCE_WISE_TRANSFER_TOKEN": "invented-token"}, clear=True),
                        mock.patch.object(wise.urllib.request, "urlopen", side_effect=self.open)]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def open(self, req, timeout=None):
        path = req.full_url.removeprefix(wise.BASE_URL)
        self.calls.append((req.method, path, json.loads(req.data) if req.data else None, req))
        if path.endswith("/profiles"):
            result = [{"id": 7, "type": "BUSINESS"}]
        elif path.endswith("/accounts/42"):
            result = {"id": 42, "profileId": 7, "active": True, "currency": "EUR", "name": "Example Ltd", "longAccountSummary": "••1234"}
        elif path.startswith("/2026Q4/transfers?"):
            result = [{"id": 9, "targetAccount": 42, "status": "outgoing_payment_sent", "sourceCurrency": "CHF", "created": "2026-10-01T00:00:00Z"}]
        elif path.endswith("/quotes"):
            result = {"id": "quote-1", "targetAmountAllowed": True, "targetAmount": 1081, "sourceCurrency": "CHF", "targetCurrency": "EUR", "sourceAmount": 1100, "rateExpirationTime": "2026-11-01T00:00:00Z", "paymentOptions": [{"payIn": "BALANCE", "disabled": False, "fee": {"total": 5}}]}
        elif path.endswith("/transfer-requirements"):
            result = []
        elif req.method == "POST" and path.endswith("/transfers"):
            result = {"id": "123456"}
        elif path.endswith("/transfers/123456"):
            result = {"id": 123456, "status": "incoming_payment_waiting", "targetAccount": 42, "targetCurrency": "EUR", "targetValue": 1081, "details": {"reference": "INV-TEST-1"}, "rateExpirationTime": "2026-11-01T00:00:00Z", "created": "2026-10-31T00:00:00Z"}
        else:
            raise AssertionError(path)
        return Response(json.dumps(result).encode())

    def run_main(self, args=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wise.main(self.args if args is None else args)
        return out.getvalue()

    def test_first_line_token_and_request_hosts(self):
        with open(wise.__file__) as f:
            self.assertEqual(f.readline().rstrip(), "#!/usr/local/bin/av inject +FINANCE_WISE_TRANSFER_TOKEN /bin/bash")
        self.run_main()
        self.assertTrue(all(call[3].full_url.startswith("https://api.wise.com/") for call in self.calls))

    def test_dry_run_is_get_only_and_prints_rules(self):
        output = self.run_main()
        self.assertTrue(all(method == "GET" for method, *_ in self.calls))
        self.assertIn("dry run: nothing sent", output)
        self.assertIn("price: known only after the quote, on --yes", output)

    def test_reviewed_set_refuses_unreviewed_paths(self):
        for method, path in (("POST", "/2026Q4/transfers/1/fund"), ("POST", "/2026Q4/payments"),
                             ("DELETE", "/2026Q4/transfers/1"), ("POST", "/2026Q4/accounts"),
                             ("POST", "/2026Q4/batch-groups"), ("PATCH", "/2026Q4/profiles/7/quotes/1"),
                             ("GET", "/2025Q4/profiles")):
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
        writes = [(method, path.split("?")[0]) for method, path, *_ in self.calls if method == "POST"]
        self.assertEqual(writes, [("POST", "/2026Q4/profiles/7/quotes"), ("POST", "/2026Q4/transfer-requirements"), ("POST", "/2026Q4/transfers")])

    def test_attempt_changes_idempotency_key(self):
        self.assertEqual(wise.transaction_id(42, "INV-TEST-1", 1), wise.transaction_id(42, "INV-TEST-1", 1))
        self.assertNotEqual(wise.transaction_id(42, "INV-TEST-1", 1), wise.transaction_id(42, "INV-TEST-1", 2))

    def test_amount_validation(self):
        for value in ("0", "-1", "1.001", "nan"):
            with self.assertRaises(SystemExit):
                wise.amount_value(value)


if __name__ == "__main__":
    unittest.main()
