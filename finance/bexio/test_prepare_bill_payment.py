"""Offline coverage for the reviewed bill-payment launcher."""

import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import launcher
from private_guard import setUpModule

pp = launcher.load("prepare_bill_payment.py")

NORMAL_IBAN = "CH9300762011623852957"
QR_IBAN = "CH4431999123000889012"
PAYMENT = {
    "type": "IBAN",
    "iban": NORMAL_IBAN,
    "name": "Example Recipient",
    "street": "Example Road",
    "house_no": "7",
    "postcode": "8000",
    "city": "Exampletown",
    "country": "CH",
    "message": "Invoice INV-TEST",
}


class PaymentFileTest(unittest.TestCase):
    def payment_file(self, data):
        folder = tempfile.mkdtemp(dir=pp.PRIVATE)
        path = os.path.join(folder, "payment.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_rejects_unknown_file_keys(self):
        with self.assertRaisesRegex(SystemExit, "unknown keys"):
            pp.load_payment(self.payment_file(dict(PAYMENT, dangerous="x")))

    def test_rejects_bad_iban_checksum(self):
        with self.assertRaisesRegex(SystemExit, "check digits"):
            pp.load_payment(
                self.payment_file(dict(PAYMENT, iban="CH9300762011623852958"))
            )

    def test_requires_every_receiver_address_field(self):
        for key in ("name", "street", "house_no", "postcode", "city", "country"):
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, key):
                pp.load_payment(
                    self.payment_file(
                        {k: v for k, v in dict(PAYMENT, **{key: ""}).items()}
                    )
                )

    def test_refuses_foreign_iban(self):
        with self.assertRaisesRegex(SystemExit, "foreign"):
            pp.load_payment(
                self.payment_file(dict(PAYMENT, iban="DE89370400440532013000"))
            )

    def test_refuses_reference_for_iban(self):
        with self.assertRaisesRegex(SystemExit, "only valid for QR"):
            pp.load_payment(
                self.payment_file(dict(PAYMENT, reference_no="RF18539007547034"))
            )

    def test_refuses_qr_without_reference_or_with_message(self):
        qr = dict(PAYMENT, type="QR", iban=QR_IBAN)
        with self.assertRaisesRegex(SystemExit, "requires reference_no"):
            pp.load_payment(self.payment_file(qr))
        with self.assertRaisesRegex(SystemExit, "message is only valid"):
            pp.load_payment(
                self.payment_file(
                    dict(qr, reference_no="210000000003139471430009017", message="x")
                )
            )

    def test_refuses_plain_iban_for_qr_and_qr_iban_for_iban(self):
        with self.assertRaisesRegex(SystemExit, "QR-IBAN"):
            pp.load_payment(self.payment_file(dict(PAYMENT, iban=QR_IBAN)))
        with self.assertRaisesRegex(SystemExit, "creditor reference"):
            pp.load_payment(
                self.payment_file(
                    dict(
                        PAYMENT,
                        type="QR",
                        message="",
                        reference_no="210000000003139471430009017",
                    )
                )
            )

    def test_plain_iban_with_valid_creditor_reference_is_accepted(self):
        payment = dict(PAYMENT, type="QR", reference_no="RF18539007547034")
        payment.pop("message")
        self.assertEqual(
            pp.load_payment(self.payment_file(payment))["iban"], NORMAL_IBAN
        )

    def test_invalid_qr_reference_is_refused(self):
        payment = dict(PAYMENT, type="QR", iban=QR_IBAN, reference_no="bad")
        payment.pop("message")
        with self.assertRaisesRegex(SystemExit, "valid QR reference"):
            pp.load_payment(self.payment_file(payment))

    def test_payment_file_must_be_private(self):
        with self.assertRaisesRegex(SystemExit, "private folder"):
            pp.load_payment(__file__)


class PlanTest(unittest.TestCase):
    def test_the_reviewed_request_set_is_exact(self):
        self.assertEqual(
            pp.REQUESTS,
            {
                ("GET", "/4.0/purchase/bills"),
                ("GET", "/4.0/purchase/bills/{id}"),
                ("PUT", "/4.0/purchase/bills/{id}/bookings/BOOKED"),
                ("GET", "/4.0/purchase/outgoing-payments"),
                ("GET", "/3.0/banking/accounts"),
                ("POST", "/4.0/purchase/outgoing-payments"),
            },
        )
        with self.assertRaises(pp.RefusedRequest):
            pp._send("POST", "/4.0/banking/payments", "token")
        with self.assertRaises(pp.RefusedRequest):
            pp._send("POST", "/4.0/purchase/outgoing-payments/send", "token")

    def test_dynamic_allowlist_only_accepts_bill_get_and_booking_put(self):
        bill_path = "/4.0/purchase/bills/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        allowed = {
            (method, path) for method, path in pp.REQUESTS if "{" not in path
        } | {
            ("GET", bill_path),
            ("PUT", bill_path + "/bookings/BOOKED"),
        }
        paths = (
            {path for _, path in allowed}
            | {path for _, path in pp.REQUESTS}
            | {
                bill_path + "/bookings/DRAFT",
                bill_path + "/send",
                bill_path + "?send=true",
                "/4.0/banking/payments",
            }
        )
        for path in paths:
            for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                with self.subTest(method=method, path=path), mock.patch.object(
                    pp.urllib.request, "urlopen"
                ) as http:
                    http.return_value.__enter__.return_value.read.return_value = b"{}"
                    if (method, path) in allowed:
                        pp._send(method, path, "token")
                        http.assert_called_once()
                    else:
                        with self.assertRaises(pp.RefusedRequest):
                            pp._send(method, path, "token")
                        http.assert_not_called()

    def test_403_stops_without_retry(self):
        with mock.patch.object(
            pp.urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError("url", 403, "denied", {}, None),
        ) as http:
            with self.assertRaisesRegex(SystemExit, "token right is missing"):
                pp._send("POST", "/4.0/purchase/outgoing-payments", "token", body={})
            http.assert_called_once()

    def test_supplier_history_reads_each_bill(self):
        bills = [
            {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
        ]
        with mock.patch.object(
            pp, "_list_bills", return_value=bills
        ) as listing, mock.patch.object(
            pp,
            "_payments",
            side_effect=[
                [{"receiver_iban": NORMAL_IBAN}],
                [{"receiver_iban": QR_IBAN}],
            ],
        ) as payments:
            self.assertEqual(pp.known_ibans("token", 12), {NORMAL_IBAN, QR_IBAN})
            listing.assert_called_once_with("token", 12)
            self.assertEqual(payments.call_count, 2)
        with self.assertRaisesRegex(SystemExit, "supplier_id"):
            pp.known_ibans("token", None)

    def test_missing_sender_fields_are_refused_before_booking(self):
        account = dict(id=4, type="bank", currency_id=1)
        with self.assertRaisesRegex(SystemExit, "iban_nr"):
            pp.validate_bank_account([account])

    def test_execution_date_default_and_future_bill_weekend(self):
        today = datetime.date(2026, 1, 5)
        bill = {"bill_date": "2026-01-17", "due_date": "2026-01-18"}
        self.assertEqual(
            pp.checked_execution_date(None, bill, today), datetime.date(2026, 1, 19)
        )
        for value in ("2026-01-17", "2026-01-18", "2025-12-01"):
            self.assertEqual(
                pp.checked_execution_date(
                    value, {"bill_date": "2026-01-01"}, datetime.date(2026, 1, 18)
                ),
                datetime.date(2026, 1, 19),
            )
        for value in (None, "not-a-date"):
            with self.assertRaises(SystemExit):
                pp.checked_execution_date(value, {"bill_date": "2026-01-01"}, today)

    def test_network_failure_never_retries_post(self):
        with mock.patch.object(
            pp.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ) as http:
            with self.assertRaisesRegex(SystemExit, "could not reach"):
                pp._send("POST", "/4.0/purchase/outgoing-payments", "token", body={})
            http.assert_called_once()

    def test_pagination_keeps_supplier_and_bill_filters(self):
        bill_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for run, path, filters, items in (
            (
                lambda: pp._list_bills("token", 12),
                "/4.0/purchase/bills",
                {"supplier_id": 12},
                {"data": [{}] * 500},
            ),
            (
                lambda: pp._payments("token", bill_id),
                "/4.0/purchase/outgoing-payments",
                {"bill_id": bill_id},
                {"data": [{}] * 500},
            ),
        ):
            with mock.patch.object(
                pp, "_send", side_effect=[items, {"data": []}]
            ) as send:
                self.assertEqual(len(run()), 500)
                self.assertEqual(
                    send.call_args_list[1],
                    mock.call(
                        "GET", path, "token", query=dict(filters, page=2, limit=500)
                    ),
                )

    def test_draft_amount_calc_null_is_refused_with_repair_instruction(self):
        with self.assertRaisesRegex(SystemExit, "create_contact_and_bill.py --repair"):
            pp.validate_bill(
                {"status": "DRAFT", "amount_calc": None, "currency_code": "CHF"}, []
            )

    def test_bill_status_currency_and_open_payment_refusals(self):
        with self.assertRaisesRegex(SystemExit, "status"):
            pp.validate_bill(
                {"status": "PAID", "amount_calc": 1081, "currency_code": "CHF"}, []
            )
        with self.assertRaisesRegex(SystemExit, "CHF"):
            pp.validate_bill(
                {"status": "BOOKED", "amount_calc": 1081, "currency_code": "EUR"}, []
            )
        with self.assertRaisesRegex(SystemExit, "open outgoing payment"):
            pp.validate_bill(
                {"status": "BOOKED", "amount_calc": 1081, "currency_code": "CHF"},
                [{"status": "PENDING"}],
            )
        pp.validate_bill(
            {"status": "BOOKED", "amount_calc": 1081, "currency_code": "CHF"},
            [{"status": "ERROR"}],
        )

    def test_sender_must_be_unique_chf_bank_account(self):
        account = dict(
            id=4,
            type="bank",
            currency_id=1,
            iban_nr=NORMAL_IBAN,
            owner="Example Sender",
            owner_address="Example Road",
            owner_zip="8000",
            owner_city="Exampletown",
        )
        self.assertEqual(pp.validate_bank_account([account]), account)
        for matches in (
            [],
            [dict(account, type="cash")],
            [dict(account, currency_id=2)],
            [account, account],
        ):
            with self.subTest(matches=matches), self.assertRaisesRegex(
                SystemExit, "CHF bank account"
            ):
                pp.validate_bank_account(matches)

    def test_execution_date_weekdays_weekends_past_today_and_bill_date(self):
        bill = {"bill_date": "2026-01-01", "due_date": "2026-01-12"}
        today = datetime.date(2026, 1, 5)
        self.assertEqual(
            pp.checked_execution_date("2026-01-14", bill, today),
            datetime.date(2026, 1, 14),
        )
        self.assertEqual(
            pp.checked_execution_date("2026-01-17", bill, today),
            datetime.date(2026, 1, 16),
        )
        self.assertEqual(
            pp.checked_execution_date("2026-01-18", bill, today),
            datetime.date(2026, 1, 16),
        )
        self.assertEqual(pp.checked_execution_date("2025-12-20", bill, today), today)
        weekend_today = datetime.date(2026, 1, 17)
        self.assertEqual(
            pp.checked_execution_date("2026-01-17", bill, weekend_today),
            datetime.date(2026, 1, 19),
        )
        self.assertGreaterEqual(
            pp.checked_execution_date(
                "2026-01-03",
                {"bill_date": "2026-01-04", "due_date": "2026-01-03"},
                today,
            ),
            datetime.date(2026, 1, 4),
        )

    def test_iban_and_qr_bodies_have_distinct_fields(self):
        bill = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "pending_amount": 1081.0}
        account = {
            "id": 4,
            "iban_nr": NORMAL_IBAN,
            "owner": "Example Sender",
            "owner_address": "Main Road 2",
            "owner_house_number": 2,
            "owner_zip": 8000,
            "owner_city": "Exampletown",
            "owner_country_code": "CH",
        }
        day = datetime.date(2026, 1, 14)
        iban = pp.payment_body(bill, account, PAYMENT, day)
        self.assertEqual(
            (
                iban["amount"],
                iban["currency_code"],
                iban["exchange_rate"],
                iban["fee_type"],
                iban["is_salary_payment"],
            ),
            (1081.0, "CHF", 1, "NO_FEE", False),
        )
        self.assertEqual(iban["sender_iban"], NORMAL_IBAN)
        self.assertEqual(
            (iban["sender_street"], iban["sender_house_no"]), ("Main Road", "2")
        )
        qr = dict(
            PAYMENT,
            type="QR",
            iban=QR_IBAN,
            message="",
            reference_no="210000000003139471430009017",
        )
        qr_body = pp.payment_body(bill, account, qr, day)
        self.assertIn("reference_no", qr_body)
        self.assertNotIn("message", qr_body)
        self.assertNotIn("fee_type", qr_body)


class MainSafetyTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=pp.PRIVATE)
        self.path = os.path.join(self.folder, "payment.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(PAYMENT, f)
        self.bill = {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "document_no": "INV-TEST-1",
            "supplier_id": 12,
            "status": "DRAFT",
            "amount_calc": 1081.0,
            "pending_amount": 1081.0,
            "currency_code": "CHF",
            "bill_date": "2026-01-01",
            "due_date": "2026-01-12",
        }
        self.created = {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}
        self.account = {
            "id": 4,
            "type": "bank",
            "currency_id": 1,
            "iban_nr": NORMAL_IBAN,
            "owner": "Example Sender",
            "owner_address": "Main Road 2",
            "owner_house_number": 2,
            "owner_zip": 8000,
            "owner_city": "Exampletown",
            "owner_country_code": "CH",
        }

    def run_plan(
        self,
        status="DRAFT",
        yes=False,
        history=None,
        send_side_effect=None,
        accept=False,
    ):
        self.bill["status"] = status
        calls = []
        outgoing = [[], [{"id": self.created["id"], "status": "PENDING"}]]

        def send(method, path, token, body=None, query=None):
            calls.append((method, path, body))
            if send_side_effect:
                result = send_side_effect(method, path, token, body, query)
                if result is not NotImplemented:
                    return result
            if method == "GET" and path.startswith("/4.0/purchase/bills/"):
                return self.bill
            if method == "POST":
                return self.created
            return None

        output = io.StringIO()
        patches = [
            mock.patch.dict(os.environ, {"BEXIO_TOKEN": "invented-token"}),
            mock.patch.object(pp, "_list_bills", return_value=[self.bill]),
            mock.patch.object(pp, "_send", side_effect=send),
            mock.patch.object(pp, "_payments", side_effect=outgoing),
            mock.patch.object(pp, "bank_accounts", return_value=[self.account]),
            mock.patch.object(
                pp, "known_ibans", return_value=set() if history is None else history
            ),
            mock.patch.object(
                pp, "checked_execution_date", return_value=datetime.date(2026, 1, 14)
            ),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(output):
                pp.main(
                    [
                        "--bill",
                        "INV-TEST-1",
                        "--payment",
                        self.path,
                        "--bank-account-id",
                        "4",
                    ]
                    + (["--yes"] if yes else [])
                    + (["--accept-new-iban"] if accept else [])
                )
        return calls, output.getvalue()

    def test_dry_run_sends_no_write(self):
        calls, output = self.run_plan()
        self.assertIn("dry run", output)
        self.assertFalse(any(method in {"PUT", "POST"} for method, _, _ in calls))

    def test_first_payment_is_printed(self):
        _, output = self.run_plan(status="BOOKED")
        self.assertIn("first payment to this IBAN", output)

    def test_different_known_iban_is_refused_unless_flag_is_set(self):
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            pp, "_list_bills", return_value=[self.bill]
        ), mock.patch.object(
            pp, "_send", return_value=self.bill
        ) as send, mock.patch.object(
            pp, "_payments", return_value=[]
        ), mock.patch.object(
            pp, "bank_accounts", return_value=[self.account]
        ), mock.patch.object(
            pp, "known_ibans", return_value={"CH1111111111111111111"}
        ):
            with self.assertRaisesRegex(SystemExit, "differs from supplier"):
                pp.main(
                    [
                        "--bill",
                        "INV-TEST-1",
                        "--payment",
                        self.path,
                        "--bank-account-id",
                        "4",
                    ]
                )
            self.assertFalse(
                any(call.args[0] in {"PUT", "POST"} for call in send.call_args_list)
            )

    def test_accept_new_iban_flag_allows_reviewed_override(self):
        calls, output = self.run_plan(yes=True, history={QR_IBAN}, accept=True)
        self.assertIn("accepted by yardmaster", output)
        self.assertEqual(sum(method == "POST" for method, _, _ in calls), 1)

    def test_non_pending_readback_is_a_failure_and_recorded(self):
        with mock.patch.object(
            pp, "_payments", return_value=[dict(self.created, status="ERROR")]
        ), mock.patch.object(pp, "_send", return_value=self.created), mock.patch.object(
            pp, "record_result"
        ) as record:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                SystemExit, "not PENDING"
            ):
                pp.prepare_payment("token", self.bill["id"], self.bill, {}, True)
        self.assertIn("status=ERROR", record.call_args.args[0])

    def test_missing_or_ambiguous_bill_never_writes(self):
        for rows in ([], [self.bill, self.bill]):
            with mock.patch.dict(
                os.environ, {"BEXIO_TOKEN": "invented-token"}
            ), mock.patch.object(
                pp, "_list_bills", return_value=rows
            ), mock.patch.object(
                pp, "_send"
            ) as send:
                with self.assertRaisesRegex(SystemExit, "expected one bill"):
                    pp.main(
                        [
                            "--bill",
                            "INV-TEST-1",
                            "--payment",
                            self.path,
                            "--bank-account-id",
                            "4",
                            "--yes",
                        ]
                    )
                send.assert_not_called()

    def test_failed_booking_never_posts_and_records_failure(self):
        def fail(method, *args):
            if method == "PUT":
                raise SystemExit("simulated booking failure")
            return NotImplemented

        with mock.patch.object(pp, "record_result") as record:
            with self.assertRaisesRegex(SystemExit, "booking failure"):
                self.run_plan(yes=True, send_side_effect=fail)
        record.assert_called_once()
        self.assertNotIn(NORMAL_IBAN, record.call_args.args[0])

    def test_draft_and_booked_write_paths(self):
        draft, _ = self.run_plan(yes=True)
        self.assertEqual(
            [method for method, _, _ in draft if method in {"PUT", "POST"}],
            ["PUT", "POST"],
        )
        booked, _ = self.run_plan(status="BOOKED", yes=True)
        self.assertEqual(
            [method for method, _, _ in booked if method in {"PUT", "POST"}], ["POST"]
        )

    def test_failed_post_rerun_skips_booking_for_already_booked_bill(self):
        calls = []

        def failed_send(method, path, token, body=None, query=None):
            calls.append(method)
            if method == "GET" and path.startswith("/4.0/purchase/bills/"):
                return self.bill
            if method == "POST":
                raise SystemExit("simulated uncertain POST")
            return None

        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "invented-token"}
        ), mock.patch.object(
            pp, "_list_bills", return_value=[self.bill]
        ), mock.patch.object(
            pp, "_send", side_effect=failed_send
        ), mock.patch.object(
            pp, "_payments", return_value=[]
        ), mock.patch.object(
            pp, "bank_accounts", return_value=[self.account]
        ), mock.patch.object(
            pp, "known_ibans", return_value=set()
        ), mock.patch.object(
            pp, "checked_execution_date", return_value=datetime.date(2026, 1, 14)
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "uncertain POST"):
                    pp.main(
                        [
                            "--bill",
                            "INV-TEST-1",
                            "--payment",
                            self.path,
                            "--bank-account-id",
                            "4",
                            "--yes",
                        ]
                    )
        self.assertEqual(calls.count("PUT"), 1)
        self.bill["status"] = "BOOKED"
        rerun, _ = self.run_plan(status="BOOKED", yes=True)
        self.assertEqual(
            [method for method, _, _ in rerun if method in {"PUT", "POST"}], ["POST"]
        )


if __name__ == "__main__":
    unittest.main()
