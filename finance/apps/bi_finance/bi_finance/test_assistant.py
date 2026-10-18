"""Offline tests for the desk assistant: the collectors, the ask flow and the OpenRouter client.

Invented data only: made-up users, names, amounts and keys, no company figure. The module imports frappe, which only
exists in the image, so a stub stands in for it (sys.modules below) and the HTTP client gets a fake opener — the
module runs on the host and is part of the depot's light gate (.yardr/check-host.sh):

    python3 -m unittest bi_finance.test_assistant

What is pinned here: the collectors read as the session user through frappe.get_list and never whole tables (counts
as one aggregate row, ten rows at most), a doctype the user may not read is left out instead of failing the ask, the
request body carries the model and the zero-retention routing, the key travels in the request header and in nothing
else, the no-key and switched-off paths say what to do, and the answer is structured JSON with links the desk can
open. The desk page and the synced JSON files are guarded by test_layout and test_sidebar.
"""

import io
import json
import re
import sys
import types
import unittest
import urllib.error
from datetime import date
from unittest import mock

USER = "tester@example.com"
KEY = "invented-openrouter-key-0000"
TODAY = date(2026, 10, 18)

ANSWER = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "text": "Two invoices are overdue.",
                        "items": [
                            {"doctype": "Sales Invoice", "name": "INV-2026-0042"}
                        ],
                    }
                )
            }
        }
    ]
}


# ---------------------------------------------------------------- the frappe stub


class Settings:
    """AI Assistant Settings as a single, with the password read the way get_password reads it."""

    def __init__(self, enabled=True, key=KEY, model="invented/model", max_tokens=777):
        self.enabled = enabled
        self.model = model
        self.max_tokens_per_answer = max_tokens
        self._key = key

    def get_password(self, fieldname, raise_exception=False):
        return self._key


class Frappe:
    """The parts of frappe assistant.py touches. A test fills rows, counts, denies and singles, then asserts
    on the recorded get_list calls."""

    class ValidationError(Exception):
        pass

    class PermissionError(Exception):
        pass

    def __init__(self):
        self.session = types.SimpleNamespace(user=USER)
        self.lists = []  # every get_list call: (doctype, kwargs)
        self.rows = {}  # doctype -> rows of a plain get_list
        self.counts = {}  # doctype -> n the aggregate row returns
        self.count_of = {}  # doctype -> callable(kwargs) -> n, when the filters decide
        self.denied = set()  # doctypes get_list refuses
        self.present = {}  # doctype -> names db.exists answers yes to
        self.singles = {}  # doctype -> field -> value
        self.readable = set()  # doctypes has_permission allows
        self.settings = Settings()
        self.thrown = []
        self.utils = types.SimpleNamespace(
            getdate=lambda *args: TODAY,
            strip_html=lambda text: re.sub(r"<[^>]*>", " ", text or ""),
        )

    def _(self, text):
        return text

    def parse_json(self, text):
        return json.loads(text) if isinstance(text, str) else text

    def whitelist(self, *args, **kwargs):
        def wrap(function):
            function.is_whitelisted = True
            return function

        return wrap

    def throw(self, message):
        self.thrown.append(message)
        raise self.ValidationError(message)

    def get_single(self, doctype):
        return self.settings

    def has_permission(self, doctype=None, ptype="read", user=None, **kwargs):
        return doctype in self.readable

    def get_list(self, doctype, **kwargs):
        self.lists.append((doctype, kwargs))
        if doctype in self.denied:
            raise self.PermissionError(doctype)
        if kwargs.get("fields") == ["count(name) as n"]:
            decide = self.count_of.get(doctype)
            return [{"n": decide(kwargs) if decide else self.counts.get(doctype, 0)}]
        return [dict(row) for row in self.rows.get(doctype, [])]

    class DB:
        def __init__(self, frappe):
            self._frappe = frappe

        def exists(self, doctype, name=None):
            return name in self._frappe.present.get(doctype, set())

        def get_single_value(self, doctype, fieldname):
            return self._frappe.singles.get(doctype, {}).get(fieldname)

    def __getattr__(self, name):
        # frappe.db is read as an attribute of the module
        if name == "db":
            self.db = Frappe.DB(self)
            return self.db
        raise AttributeError(name)


FRAPPE = Frappe()
sys.modules.setdefault("frappe", FRAPPE)

# imported after the stub is in place: assistant.py does `import frappe` and gets it
from bi_finance import assistant, assistant_client  # noqa: E402


def last_calls(doctype):
    return [kwargs for called, kwargs in FRAPPE.lists if called == doctype]


def entry(result, key):
    return next(found for found in result if found["key"] == key)


class FakeOpener:
    """Stands in for urlopen: answers with the given JSON body, or raises the given error."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode("utf-8"))


# ---------------------------------------------------------------- the client


class ClientRequest(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener(ANSWER)
        assistant_client.answer(
            key=KEY,
            model="invented/model",
            max_tokens=777,
            message="What is open for me?",
            history=[{"role": "assistant", "content": "earlier"}],
            data=[{"key": "todos", "count": 1}],
            opener=self.opener,
        )

    def body(self):
        return json.loads(self.opener.requests[0].data)

    def test_the_key_travels_in_the_header_alone(self):
        self.assertEqual(
            self.opener.requests[0].get_header("Authorization"), "Bearer " + KEY
        )
        self.assertNotIn(KEY.encode(), self.opener.requests[0].data)

    def test_the_body_carries_the_model_and_the_token_cap(self):
        body = self.body()
        self.assertEqual(body["model"], "invented/model")
        self.assertEqual(body["max_tokens"], 777)

    def test_the_body_carries_the_zero_retention_routing(self):
        self.assertEqual(
            self.body()["provider"],
            {"allow_fallbacks": False, "zdr": True, "data_collection": "deny"},
        )

    def test_the_messages_run_system_then_history_then_question(self):
        messages = self.body()["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("DATA", messages[0]["content"])
        self.assertIn("todos", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "assistant", "content": "earlier"})
        self.assertEqual(
            messages[-1], {"role": "user", "content": "What is open for me?"}
        )

    def test_the_call_gives_up_after_thirty_seconds(self):
        self.assertEqual(assistant_client.TIMEOUT_SECONDS, 30)
        self.assertEqual(self.opener.timeouts, [30])


class ClientParse(unittest.TestCase):
    def parse(self, content):
        return assistant_client.parse_answer(content)

    def test_a_plain_json_answer_is_read(self):
        self.assertEqual(
            self.parse('{"text": "hi", "items": []}'), {"text": "hi", "items": []}
        )

    def test_a_fenced_answer_is_read(self):
        self.assertEqual(
            self.parse('```json\n{"text": "hi", "items": []}\n```'),
            {"text": "hi", "items": []},
        )

    def test_prose_around_the_object_is_read(self):
        self.assertEqual(
            self.parse('Sure: {"text": "hi", "items": []} — done.'),
            {"text": "hi", "items": []},
        )

    def test_items_left_out_is_an_empty_list(self):
        self.assertEqual(self.parse('{"text": "hi"}'), {"text": "hi", "items": []})

    def test_a_shape_that_cannot_be_linked_is_refused(self):
        for content in (
            "no json at all",
            '["a list"]',
            '{"text": 5, "items": []}',
            '{"text": "hi", "items": "one"}',
        ):
            with self.assertRaises(assistant_client.AssistantError):
                self.parse(content)


class ClientErrors(unittest.TestCase):
    def test_unusable_success_response_shapes_are_assistant_errors(self):
        for response in (
            [],
            None,
            {},
            {"choices": []},
            {"choices": "wrong"},
            {"choices": [{}]},
            {"choices": [{"message": "wrong"}]},
            {"choices": [{"message": {"content": []}}]},
        ):
            with self.subTest(response=response):
                with self.assertRaises(assistant_client.AssistantError):
                    assistant_client.answer(
                        key=KEY,
                        model="m",
                        max_tokens=1,
                        message="q",
                        history=[],
                        data=[],
                        opener=FakeOpener(response),
                    )

    def test_an_http_error_names_the_status_and_not_the_body(self):
        opener = FakeOpener(
            error=urllib.error.HTTPError(
                assistant_client.URL,
                402,
                "needs credits",
                None,
                io.BytesIO(b"secret bits"),
            )
        )
        with self.assertRaises(assistant_client.AssistantError) as raised:
            assistant_client.answer(
                key=KEY,
                model="m",
                max_tokens=1,
                message="q",
                history=[],
                data=[],
                opener=opener,
            )
        self.assertEqual(str(raised.exception), "HTTP 402 from OpenRouter")

    def test_a_network_failure_names_no_url(self):
        opener = FakeOpener(error=urllib.error.URLError("no route"))
        with self.assertRaises(assistant_client.AssistantError) as raised:
            assistant_client.answer(
                key=KEY,
                model="m",
                max_tokens=1,
                message="q",
                history=[],
                data=[],
                opener=opener,
            )
        self.assertIn("URLError", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))

    def test_an_error_object_in_a_success_answer_is_a_refusal(self):
        opener = FakeOpener({"error": {"message": "account details here"}})
        with self.assertRaises(assistant_client.AssistantError) as raised:
            assistant_client.answer(
                key=KEY,
                model="m",
                max_tokens=1,
                message="q",
                history=[],
                data=[],
                opener=opener,
            )
        self.assertEqual(str(raised.exception), "OpenRouter refused the request")


# ---------------------------------------------------------------- the ask flow


class Ask(unittest.TestCase):
    def setUp(self):
        FRAPPE.__init__()  # a fresh stub for every test
        FRAPPE.settings = Settings()
        FRAPPE.present["DocType"] = {"Sales Invoice", "Purchase Invoice", "ToDo"}

    def ask(self, message="What is open for me?", history=None):
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={"text": "two are overdue", "items": []},
        ) as client:
            result = assistant.ask(message, history)
        return result, client

    def existing(self):
        # the documents db.exists answers yes to, so an item's link is only built for what exists
        FRAPPE.present.update(
            {
                "Sales Invoice": {"INV-2026-0042"},
                "Purchase Invoice": {"PINV-2026-0007"},
            }
        )

    def test_switched_off_says_what_to_do_and_reads_nothing(self):
        FRAPPE.settings = Settings(enabled=False)
        with self.assertRaises(Frappe.ValidationError) as raised:
            assistant.ask("What is open for me?")
        self.assertIn("switched off", str(raised.exception))
        self.assertIn("AI Assistant Settings", str(raised.exception))

    def test_no_key_says_what_to_do_and_calls_nothing(self):
        FRAPPE.settings = Settings(key=None)
        with mock.patch.object(assistant_client, "answer") as client:
            with self.assertRaises(Frappe.ValidationError) as raised:
                assistant.ask("What is open for me?")
        self.assertIn("OpenRouter API key", str(raised.exception))
        client.assert_not_called()

    def test_the_answer_is_the_structured_json_with_links(self):
        self.existing()
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={
                "text": "Two invoices are overdue.",
                "items": [
                    {"doctype": "Sales Invoice", "name": "INV-2026-0042"},
                    {"doctype": "Purchase Invoice", "name": "PINV-2026-0007"},
                ],
            },
        ) as client:
            result = assistant.ask("What is open for me?")
        self.assertEqual(result["text"], "Two invoices are overdue.")
        self.assertEqual(
            [item["link"] for item in result["items"]],
            [
                "/app/sales-invoice/INV-2026-0042",
                "/app/purchase-invoice/PINV-2026-0007",
            ],
        )
        client.assert_called_once()

    def test_the_key_is_nowhere_in_the_response(self):
        result, _ = self.ask()
        self.assertNotIn(KEY, json.dumps(result))

    def test_the_settings_model_and_cap_win_and_the_defaults_fill_the_rest(self):
        _, client = self.ask()
        self.assertEqual(client.call_args.kwargs["model"], "invented/model")
        self.assertEqual(client.call_args.kwargs["max_tokens"], 777)
        FRAPPE.settings = Settings(model="", max_tokens=0)
        _, client = self.ask()
        self.assertEqual(client.call_args.kwargs["model"], assistant.DEFAULT_MODEL)
        self.assertEqual(
            client.call_args.kwargs["max_tokens"], assistant.DEFAULT_MAX_TOKENS
        )

    def test_the_collectors_data_goes_to_the_client_as_the_user(self):
        _, client = self.ask()
        self.assertEqual(client.call_args.kwargs["data"], assistant.collect())
        self.assertEqual(client.call_args.kwargs["message"], "What is open for me?")

    def test_a_blank_question_is_refused_before_anything_is_read(self):
        with mock.patch.object(assistant_client, "answer") as client:
            with self.assertRaises(Frappe.ValidationError):
                assistant.ask("   ")
        client.assert_not_called()

    def test_a_long_question_is_capped(self):
        _, client = self.ask("word " * 2000)
        self.assertLessEqual(
            len(client.call_args.kwargs["message"]), assistant.MESSAGE_CHARS
        )

    def test_the_history_keeps_the_last_turns_of_the_known_roles_capped(self):
        turns = [
            {"role": role, "content": f"{role}-{i}"}
            for i in range(15)
            for role in ("user", "assistant")
        ]
        turns.insert(0, {"role": "tool", "content": "junk"})
        turns.append({"role": "assistant", "content": "word " * 2000})
        _, client = self.ask(history=turns)
        history = client.call_args.kwargs["history"]
        self.assertEqual(assistant.HISTORY_MESSAGES, 24)
        self.assertEqual(len(history), assistant.HISTORY_MESSAGES)
        self.assertTrue(all(turn["role"] in ("user", "assistant") for turn in history))
        self.assertEqual(len(history[-1]["content"]), assistant.MESSAGE_CHARS)
        self.assertEqual(history[0]["content"], "assistant-3")

    def test_history_as_the_client_sends_it_is_json_text(self):
        _, client = self.ask(
            history=json.dumps([{"role": "user", "content": "earlier"}])
        )
        self.assertEqual(
            client.call_args.kwargs["history"], [{"role": "user", "content": "earlier"}]
        )

    def test_items_the_model_invents_are_dropped_not_linked(self):
        self.existing()
        FRAPPE.present["Purchase Invoice"] = {f"PINV-2026-{n:04d}" for n in range(12)}
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={
                "text": "x",
                "items": [
                    {"doctype": "Not A Doctype", "name": "X"},
                    {"doctype": "Sales Invoice", "name": ""},
                    {"doctype": "Sales Invoice", "name": "INV-0000-0000"},
                    "not a dict",
                    {"doctype": "Sales Invoice", "name": "INV-2026-0042"},
                    {"doctype": "Sales Invoice", "name": "INV-2026-0042"},
                ]
                + [
                    {"doctype": "Purchase Invoice", "name": f"PINV-2026-{n:04d}"}
                    for n in range(12)
                ],
            },
        ):
            result = assistant.ask("q")
        self.assertEqual(len(result["items"]), assistant.TOP)
        self.assertEqual(result["items"][0]["link"], "/app/sales-invoice/INV-2026-0042")
        self.assertEqual(
            len([item for item in result["items"] if item["name"] == "INV-2026-0042"]),
            1,
        )
        # an invented name under a doctype that exists is not linked either: the document is checked too
        self.assertNotIn(
            "/app/sales-invoice/INV-0000-0000",
            [item["link"] for item in result["items"]],
        )

    def test_an_openrouter_failure_is_a_clear_message_without_the_key(self):
        with mock.patch.object(
            assistant_client,
            "answer",
            side_effect=assistant_client.AssistantError("HTTP 502 from OpenRouter"),
        ):
            with self.assertRaises(Frappe.ValidationError) as raised:
                assistant.ask("What is open for me?")
        self.assertIn("HTTP 502", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))


# ---------------------------------------------------------------- the collectors


class Collectors(unittest.TestCase):
    def setUp(self):
        FRAPPE.__init__()
        FRAPPE.present["DocType"] = {"Sales Invoice", "Purchase Invoice", "ToDo"}
        FRAPPE.readable = {"Wise Settings", "PayPal Settings"}
        self.result = None

    def collect(self, today=TODAY, registry=None):
        self.result = assistant.collect(today=today, registry=registry)
        return self.result

    def test_the_registry_holds_the_beads_collectors_under_stable_keys(self):
        self.assertEqual(
            [entry["key"] for entry in assistant.COLLECTORS],
            [
                "todos",
                "drafts",
                "sales_overdue",
                "purchase_due",
                "bank_unreconciled",
                "vat_declaration",
                "payroll_month",
                "syncs",
            ],
        )

    def test_the_registry_takes_part_2s_collectors(self):
        def extra(user, today):
            return {"count": 1, "top": []}

        registered = assistant.collector("extra", "An invented collector")(extra)
        self.addCleanup(assistant.COLLECTORS.pop)
        self.assertIn("extra", [entry["key"] for entry in assistant.COLLECTORS])
        subset = assistant.collect(
            registry=[e for e in assistant.COLLECTORS if e["key"] == "extra"]
        )
        self.assertEqual([entry["key"] for entry in subset], ["extra"])
        self.assertEqual(registered, extra)

    def test_todos_are_read_as_the_session_user(self):
        FRAPPE.counts["ToDo"] = 2
        FRAPPE.rows["ToDo"] = [
            {
                "name": "TODO-2026-0007",
                "description": "<p>Send the <b>invented</b> reminder</p>",
                "priority": "High",
                "date": "2026-10-19",
                "reference_type": "Sales Invoice",
                "reference_name": "INV-2026-0042",
            }
        ]
        result = self.collect()
        todos = entry(result, "todos")
        self.assertEqual(todos["count"], 2)
        self.assertEqual(todos["top"][0]["description"], "Send the invented reminder")
        for kwargs in last_calls("ToDo"):
            self.assertEqual(kwargs["filters"]["allocated_to"], USER)
            self.assertEqual(kwargs["filters"]["status"], "Open")
            self.assertLessEqual(kwargs["limit_page_length"], assistant.TOP)

    def test_drafts_carry_per_doctype_counts_and_the_ten_most_recent(self):
        FRAPPE.counts = {
            "Sales Invoice": 2,
            "Purchase Invoice": 0,
            "Payment Entry": 0,
            "Journal Entry": 0,
            "Salary Slip": 0,
            "VAT Declaration": 1,
        }
        FRAPPE.rows["Sales Invoice"] = [
            {"name": "INV-2026-0042", "modified": "2026-10-10 10:00:00"},
            {"name": "INV-2026-0043", "modified": "2026-10-11 10:00:00"},
        ]
        FRAPPE.rows["VAT Declaration"] = [
            {"name": "MWST 2026Q3", "modified": "2026-10-12 10:00:00"}
        ]
        drafts = entry(self.collect(), "drafts")
        self.assertEqual(drafts["count"], 3)
        self.assertEqual(drafts["by_doctype"]["Sales Invoice"], 2)
        self.assertEqual(drafts["by_doctype"]["Payment Entry"], 0)
        self.assertEqual(
            [(row["doctype"], row["name"]) for row in drafts["top"]],
            [
                ("VAT Declaration", "MWST 2026Q3"),
                ("Sales Invoice", "INV-2026-0043"),
                ("Sales Invoice", "INV-2026-0042"),
            ],
        )
        for kwargs in last_calls("Sales Invoice")[:2]:
            self.assertEqual(kwargs["filters"], {"docstatus": 0})
            self.assertLessEqual(kwargs["limit_page_length"], assistant.TOP)

    def test_drafts_are_the_ten_most_recent_across_doctypes(self):
        for index, doctype in enumerate(assistant.DRAFT_DOCTYPES):
            FRAPPE.counts[doctype] = 10
            FRAPPE.rows[doctype] = [
                {
                    "name": "Invented-{}-{:02d}".format(index, row),
                    "modified": "2026-10-{:02d} 00:00:00".format(index + row + 1),
                }
                for row in range(assistant.TOP)
            ]
        drafts = entry(self.collect(), "drafts")
        self.assertEqual(len(drafts["top"]), assistant.TOP)
        self.assertEqual(drafts["top"][0]["doctype"], assistant.DRAFT_DOCTYPES[-1])
        self.assertEqual(drafts["top"][-1]["doctype"], assistant.DRAFT_DOCTYPES[-1])

    def test_drafts_cap_the_combined_list_across_doctypes(self):
        for index, doctype in enumerate(assistant.DRAFT_DOCTYPES):
            FRAPPE.counts[doctype] = 12
            FRAPPE.rows[doctype] = [
                {"name": "Invented-{}-{:02d}".format(index, row)}
                for row in range(assistant.TOP)
            ]
        drafts = entry(self.collect(), "drafts")
        self.assertEqual(drafts["count"], 12 * len(assistant.DRAFT_DOCTYPES))
        self.assertEqual(len(drafts["top"]), assistant.TOP)

    def test_overdue_sales_invoices_are_unpaid_and_past_the_due_date(self):
        FRAPPE.counts["Sales Invoice"] = 1
        FRAPPE.rows["Sales Invoice"] = [
            {
                "name": "INV-2026-0042",
                "customer": "Invented Customer AG",
                "due_date": "2026-10-01",
                "outstanding_amount": 417.29,
            }
        ]
        overdue = entry(self.collect(), "sales_overdue")
        self.assertEqual(overdue["count"], 1)
        filters = last_calls("Sales Invoice")[-2]["filters"]
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters["outstanding_amount"], [">", 0])
        self.assertEqual(filters["due_date"], ["<", TODAY])

    def test_purchase_invoices_reach_fourteen_days_ahead(self):
        FRAPPE.counts["Purchase Invoice"] = 1
        FRAPPE.rows["Purchase Invoice"] = [
            {
                "name": "PINV-2026-0007",
                "supplier": "Invented Supplier GmbH",
                "due_date": "2026-10-30",
                "outstanding_amount": 96.4,
            }
        ]
        due = entry(self.collect(), "purchase_due")
        self.assertEqual(due["count"], 1)
        filters = last_calls("Purchase Invoice")[-2]["filters"]
        self.assertEqual(filters["due_date"], ["<=", date(2026, 11, 1)])
        self.assertEqual(filters["outstanding_amount"], [">", 0])

    def test_unreconciled_bank_transactions_are_counted_per_account(self):
        FRAPPE.counts["Bank Transaction"] = 4
        FRAPPE.rows["Bank Transaction"] = [
            {"bank_account": "Wise CHF - Invented Bank", "n": 3},
            {"bank_account": "Post - Invented Bank", "n": 1},
        ]
        bank = entry(self.collect(), "bank_unreconciled")
        self.assertEqual(bank["count"], 4)
        self.assertEqual(
            bank["top"],
            [
                {"bank_account": "Wise CHF - Invented Bank", "n": 3},
                {"bank_account": "Post - Invented Bank", "n": 1},
            ],
        )
        kwargs = last_calls("Bank Transaction")[0]
        self.assertEqual(kwargs["filters"]["status"], "Unreconciled")
        self.assertEqual(kwargs["group_by"], "bank_account")

    def test_unreconciled_bank_accounts_are_capped_at_ten(self):
        FRAPPE.counts["Bank Transaction"] = assistant.TOP + 2
        FRAPPE.rows["Bank Transaction"] = [
            {"bank_account": "Invented Bank {}".format(index), "n": 1}
            for index in range(assistant.TOP + 2)
        ]
        bank = entry(self.collect(), "bank_unreconciled")
        self.assertEqual(bank["count"], assistant.TOP + 2)
        self.assertEqual(len(bank["top"]), assistant.TOP)
        self.assertEqual(last_calls("Bank Transaction")[0]["limit_page_length"], assistant.TOP)

    def test_the_last_closed_quarter_is_what_the_declaration_must_cover(self):
        self.assertEqual(
            assistant._last_closed_quarter(date(2026, 10, 18)),
            (date(2026, 7, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            assistant._last_closed_quarter(date(2026, 1, 5)),
            (date(2025, 10, 1), date(2025, 12, 31)),
        )

    def test_a_covering_declaration_answers_the_vat_collector(self):
        FRAPPE.counts["VAT Declaration"] = 12
        FRAPPE.rows["VAT Declaration"] = [{"name": "MWST 2026Q3", "docstatus": 1}]
        vat = entry(self.collect(), "vat_declaration")
        self.assertEqual(vat["count"], 12)
        self.assertFalse(vat["missing"])
        self.assertEqual(vat["period"], "2026-07-01 to 2026-09-30")
        filters = last_calls("VAT Declaration")[-1]["filters"]
        self.assertEqual(filters["start_date"], ["<=", date(2026, 7, 1)])
        self.assertEqual(filters["end_date"], [">=", date(2026, 9, 30)])

    def test_a_missing_declaration_is_reported_as_missing(self):
        vat = entry(self.collect(), "vat_declaration")
        self.assertEqual(vat["count"], 0)
        self.assertTrue(vat["missing"])

    def test_payroll_month_counts_the_months_slips(self):
        def slip_count(kwargs):
            filters = kwargs["filters"]
            if isinstance(filters, list) and filters[0][2] == 1:
                return 2
            if isinstance(filters, list) and filters[0][2] == 0:
                return 1
            return 0

        FRAPPE.count_of["Salary Slip"] = slip_count
        FRAPPE.rows["Salary Slip"] = [
            {
                "name": "SAL-2026-0003",
                "employee": "HR-EMP-0003",
                "employee_name": "Invented Person",
                "docstatus": 0,
            }
        ]
        payroll = entry(self.collect(), "payroll_month")
        self.assertEqual(payroll["month"], "2026-10")
        self.assertEqual(payroll["submitted"], 2)
        self.assertEqual(payroll["drafts"], 1)
        self.assertEqual(payroll["count"], 3)
        bounds = last_calls("Salary Slip")[1]["filters"]
        self.assertIn(["start_date", ">=", date(2026, 10, 1)], bounds)
        self.assertIn(["start_date", "<=", date(2026, 10, 31)], bounds)
        self.assertEqual(
            last_calls("Salary Slip")[-1]["filters"][0], ["docstatus", "<", 2]
        )

    def test_the_sync_collector_reports_the_feeds_the_user_can_read(self):
        FRAPPE.singles = {
            "Wise Settings": {"last_sync": "2026-10-18 08:00:00"},
            "PayPal Settings": {"last_sync": None},
        }
        syncs = entry(self.collect(), "syncs")
        self.assertEqual(syncs["count"], 1)
        self.assertEqual(
            syncs["top"],
            [
                {"feed": "Wise", "last_sync": "2026-10-18 08:00:00"},
                {"feed": "PayPal", "last_sync": None},
            ],
        )
        self.assertIn("bexio", syncs["note"])

    def test_a_feed_the_user_cannot_read_is_skipped(self):
        FRAPPE.readable = set()
        FRAPPE.singles = {"Wise Settings": {"last_sync": "2026-10-18 08:00:00"}}
        syncs = entry(self.collect(), "syncs")
        self.assertEqual(syncs["count"], 0)
        self.assertEqual(syncs["top"], [])

    def test_a_doctype_the_user_may_not_read_is_left_out_not_fatal(self):
        FRAPPE.denied.add("Salary Slip")
        result = self.collect()
        payroll = entry(result, "payroll_month")
        self.assertIsNone(payroll["count"])
        self.assertEqual(payroll["top"], [])
        self.assertIn("note", payroll)
        self.assertEqual(len(result), len(assistant.COLLECTORS))

    def test_the_data_block_is_json_serializable(self):
        FRAPPE.counts["ToDo"] = 1
        FRAPPE.rows["ToDo"] = [
            {
                "name": "TODO-2026-0007",
                "description": "invented",
                "priority": "High",
                "date": "2026-10-19",
                "reference_type": "Sales Invoice",
                "reference_name": "INV-2026-0042",
            }
        ]
        json.dumps(self.collect())


if __name__ == "__main__":
    unittest.main()
