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

import http.client
import io
import json
import re
import sys
import types
import unittest
import urllib.error
from datetime import date, datetime
from unittest import mock

import bi_finance

USER = "tester@example.com"
KEY = "invented-openrouter-key-0000"
COUNT = {"COUNT": "name", "as": "n"}  # the aggregate field Frappe 16 accepts
TODAY = date(2026, 10, 18)
NOW = datetime(
    2026, 10, 18, 12, 0, 0
)  # the stub clock the staleness of a feed is measured against (assistant._now_utc)

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
    """AI Assistant Settings as a single, with the password read the way get_password reads it.

    ledger_from defaults to a date in the past, so the ask tests pin the bare answer; the banner tests set the ledger
    date (2027-01-01, the doctype's default) explicitly.
    """

    def __init__(
        self,
        enabled=True,
        key=KEY,
        model="invented/model",
        max_tokens=777,
        ledger_from="2026-01-01",
    ):
        self.enabled = enabled
        self.model = model
        self.max_tokens_per_answer = max_tokens
        self.ledger_actions_from = ledger_from
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
        self.groups = {}  # doctype -> rows of a grouped read (fields with COUNT)
        self.groups_of = (
            {}
        )  # doctype -> callable(kwargs) -> grouped rows, when the filters decide
        self.denied = set()  # doctypes get_list refuses
        self.denied_rows = set()
        self.present = {}  # doctype -> names db.exists answers yes to
        self.singles = {}  # doctype -> field -> value
        self.readable = set()  # doctypes has_permission allows
        self.unreadable_docs = set()
        self.values = (
            {}
        )  # (doctype, name) -> value or tuple of values, what db.get_value answers
        self.tables = set()  # doctypes db.table_exists answers yes to
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
        return (
            doctype in self.readable
            and (
                doctype,
                kwargs.get("doc"),
            )
            not in self.unreadable_docs
        )

    def get_list(self, doctype, **kwargs):
        self.lists.append((doctype, kwargs))
        if doctype in self.denied:
            raise self.PermissionError(doctype)
        for field in kwargs.get("fields") or []:
            # Frappe 16 refuses SQL functions written as strings in SELECT
            if isinstance(field, str) and "(" in field:
                raise self.ValidationError(
                    f"SQL functions are not allowed as strings: {field}"
                )
        if kwargs.get("fields") == [COUNT]:
            decide = self.count_of.get(doctype)
            return [{"n": decide(kwargs) if decide else self.counts.get(doctype, 0)}]
        fields = kwargs.get("fields") or []
        if isinstance(fields, list) and COUNT in fields:
            decide = self.groups_of.get(doctype)
            rows = [
                dict(row)
                for row in (decide(kwargs) if decide else self.groups.get(doctype, []))
            ]
            limit = kwargs.get("limit_page_length")
            return rows[:limit] if limit else rows
        if doctype in self.denied_rows:
            raise self.PermissionError(doctype)
        rows = [dict(row) for row in self.rows.get(doctype, [])]
        if all(isinstance(field, str) and " " not in field for field in fields):
            rows = [{field: row.get(field) for field in fields} for row in rows]
        return rows

    class DB:
        def __init__(self, frappe):
            self._frappe = frappe

        def exists(self, doctype, name=None):
            return name in self._frappe.present.get(doctype, set())

        def get_single_value(self, doctype, fieldname):
            return self._frappe.singles.get(doctype, {}).get(fieldname)

        def get_value(self, doctype, name, fieldnames=None):
            value = self._frappe.values.get((doctype, name))
            if isinstance(fieldnames, (list, tuple)):
                return tuple(value) if value is not None else None
            return value

        def table_exists(self, doctype):
            return doctype in self._frappe.tables

    def __getattr__(self, name):
        # frappe.db is read as an attribute of the module
        if name == "db":
            self.db = Frappe.DB(self)
            return self.db
        raise AttributeError(name)


FRAPPE = Frappe()
sys.modules.setdefault("frappe", FRAPPE)


class JobModule(types.ModuleType):
    """Stands in for bi_finance.bank_match_job: the match job's own readers, recording their calls.

    The real module pulls erpnext at its import, so assistant.py imports it lazily and the tests put this stub in its
    place; the reads behind the answer are then exactly the ones the tests name.
    """

    LINE_FIELDS = [
        "name",
        "date",
        "deposit",
        "withdrawal",
        "description",
        "reference_number",
        "transaction_id",
        "end_to_end_id",
        "currency",
        "company",
    ]

    def __init__(self):
        super().__init__("bi_finance.bank_match_job")
        self.reset()

    def reset(self):
        self.docs = []
        self.payments = []
        self.rules = []
        self.calls = []
        self.users = []

    def _open_documents(self, company, currency, user=None):
        self.calls.append(("documents", company, currency))
        self.users.append(user)
        return [dict(row) for row in self.docs]

    def _open_payments(self, company, currency, gl_account, user=None):
        self.calls.append(("payments", company, currency, gl_account))
        self.users.append(user)
        return [dict(row) for row in self.payments]

    def _rules(self, company, user=None):
        self.calls.append(("rules", company))
        self.users.append(user)
        return list(self.rules)


JOB = JobModule()
sys.modules.setdefault("bi_finance.bank_match_job", JOB)

# imported after the stub is in place: assistant.py does `import frappe` and gets it
from bi_finance import assistant, assistant_client, bank_match  # noqa: E402
from bi_finance.reference import creditor_reference  # noqa: E402


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


class DisconnectedResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        raise http.client.RemoteDisconnected("connection closed")


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

    def test_a_banner_travels_in_the_system_prompt_of_the_request(self):
        assistant_client.answer(
            key=KEY,
            model="invented/model",
            max_tokens=777,
            message="What is open for me?",
            history=[],
            data=[],
            banner="invented banner line",
            opener=self.opener,
        )
        system = json.loads(self.opener.requests[-1].data)["messages"][0]["content"]
        self.assertIn(
            'Start every answer with this exact first line: "invented banner line"',
            system,
        )


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

    def test_a_shape_that_cannot_be_linked_is_refused(self):
        for content in (
            "no json at all",
            '["a list"]',
            '{"items": []}',
            '{"text": "", "items": []}',
            '{"text": 5, "items": []}',
            '{"text": "   ", "items": []}',
            '{"text": "hi", "items": "one"}',
            '{"text": "hi"}',
            '{"text": "hi", "items": null}',
        ):
            with self.assertRaises(assistant_client.AssistantError):
                self.parse(content)


class ClientErrors(unittest.TestCase):
    def test_disconnect_during_response_read_reaches_ask_as_clear_error(self):
        real_answer = assistant_client.answer

        def disconnected(**kwargs):
            return real_answer(
                **kwargs,
                opener=lambda request, timeout: DisconnectedResponse(),
            )

        with mock.patch.object(assistant_client, "answer", side_effect=disconnected):
            with self.assertRaises(Frappe.ValidationError) as raised:
                assistant.ask("What is open for me?")
        self.assertIn("RemoteDisconnected", str(raised.exception))
        self.assertIn("no usable answer from OpenRouter", str(raised.exception))

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
        FRAPPE.readable.update({"Sales Invoice", "Purchase Invoice"})
        FRAPPE.present.update(
            {
                "Sales Invoice": {"INV-2026-0042", "INV-2026-9999"},
                "Purchase Invoice": {"PINV-2026-0007"},
            }
        )
        FRAPPE.rows["Sales Invoice"] = [{"name": "INV-2026-0042"}]
        FRAPPE.rows["Purchase Invoice"] = [{"name": "PINV-2026-0007"}]

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

    def test_only_documents_in_collector_data_are_linked(self):
        self.existing()
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={
                "text": "Two invoices.",
                "items": [
                    {"doctype": "Sales Invoice", "name": "INV-2026-0042"},
                    {"doctype": "Sales Invoice", "name": "INV-2026-9999"},
                ],
            },
        ):
            result = assistant.ask("What is open for me?")
        self.assertEqual([item["name"] for item in result["items"]], ["INV-2026-0042"])

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
        FRAPPE.rows["Purchase Invoice"] = [
            {"name": f"PINV-2026-{n:04d}"} for n in range(12)
        ]
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

    def test_items_without_session_user_read_permission_are_dropped(self):
        self.existing()
        FRAPPE.unreadable_docs.add(("Sales Invoice", "INV-2026-0042"))
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={
                "text": "x",
                "items": [{"doctype": "Sales Invoice", "name": "INV-2026-0042"}],
            },
        ):
            result = assistant.ask("q")
        self.assertEqual(result["items"], [])

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


# ---------------------------------------------------------------- the ledger banner


class Banner(unittest.TestCase):
    """The ledger line: before the ledger date every answer starts with it, from the date on it is gone."""

    BANNER = (
        "bexio leads until 1.1.2027; ERPNext is the parallel copy, "
        "the assistant books nothing before then."
    )

    def setUp(self):
        FRAPPE.__init__()
        FRAPPE.present["DocType"] = {"Sales Invoice", "Purchase Invoice", "ToDo"}

    def ask(self, text="two are overdue"):
        with mock.patch.object(
            assistant_client,
            "answer",
            return_value={"text": text, "items": []},
        ) as client:
            result = assistant.ask("What is open for me?")
        return result, client

    def test_before_the_ledger_date_every_answer_starts_with_the_line(self):
        FRAPPE.settings = Settings(ledger_from="2027-01-01")
        result, client = self.ask()
        self.assertEqual(client.call_args.kwargs["banner"], self.BANNER)
        self.assertTrue(result["text"].startswith(self.BANNER + "\n"))

    def test_the_model_saying_the_line_does_not_duplicate_it(self):
        FRAPPE.settings = Settings(ledger_from="2027-01-01")
        result, _ = self.ask(self.BANNER + "\ntwo are overdue")
        self.assertEqual(result["text"].count(self.BANNER), 1)
        self.assertTrue(result["text"].endswith("two are overdue"))

    def test_from_the_ledger_date_on_there_is_no_line(self):
        # the stub clock says 2026-10-18, so the boundary itself pins: on the ledger date the line is gone
        FRAPPE.settings = Settings(ledger_from=TODAY.isoformat())
        result, client = self.ask()
        self.assertIsNone(client.call_args.kwargs["banner"])
        self.assertEqual(result["text"], "two are overdue")

    def test_an_empty_setting_counts_as_the_default_date(self):
        FRAPPE.settings = Settings(ledger_from="")
        _, client = self.ask()
        self.assertEqual(client.call_args.kwargs["banner"], self.BANNER)

    def test_the_line_is_a_rule_of_the_system_prompt_too(self):
        prompt = assistant_client.system_prompt([], banner=self.BANNER)
        self.assertIn(
            'Start every answer with this exact first line: "' + self.BANNER + '"',
            prompt,
        )
        self.assertIn("DATA", prompt)
        self.assertNotIn("Start every answer", assistant_client.system_prompt([]))


# ---------------------------------------------------------------- the collectors


class Collectors(unittest.TestCase):
    def setUp(self):
        FRAPPE.__init__()
        FRAPPE.present["DocType"] = {"Sales Invoice", "Purchase Invoice", "ToDo"}
        FRAPPE.readable = {"Wise Settings", "PayPal Settings"}
        # the clock of the feed staleness: naive UTC, as wise.py and paypal.py write last_sync
        clock = mock.patch.object(assistant, "_now_utc", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        JOB.reset()
        self._saved_job = getattr(bi_finance, "bank_match_job", None)
        bi_finance.bank_match_job = JOB
        self.addCleanup(self._restore_job)
        self.result = None

    def _restore_job(self):
        if self._saved_job is None:
            if hasattr(bi_finance, "bank_match_job"):
                del bi_finance.bank_match_job
        else:
            bi_finance.bank_match_job = self._saved_job

    def collect(self, today=TODAY, registry=None):
        self.result = assistant.collect(today=today, registry=registry)
        return self.result

    def bank_line(self, name, day, amount, description="", reference=None):
        """A Bank Transaction row as the line read returns it, before the collector sets currency and company."""
        return {
            "name": name,
            "date": day,
            "deposit": amount if amount > 0 else 0.0,
            "withdrawal": -amount if amount < 0 else 0.0,
            "description": description,
            "reference_number": reference or "",
            "transaction_id": None,
            "end_to_end_id": None,
            "currency": "CHF",
            "company": "Invented Company AG",
            "bank_account": "Invented Bank CHF",
        }

    def job_doc(
        self,
        name,
        direction,
        amount,
        party="Invented Customer AG",
        day=None,
        reference=None,
    ):
        """An open invoice as the match job's reader returns it."""
        return {
            "doctype": "Sales Invoice",
            "name": name,
            "party_name": party,
            "direction": direction,
            "amount": amount,
            "currency": "CHF",
            "date": day or "2026-09-24",
            "reference": reference,
        }

    def test_the_registry_holds_the_task_ids_as_stable_keys(self):
        self.assertEqual(
            [entry["key"] for entry in assistant.COLLECTORS],
            [
                "todos",
                "drafts",
                "sales.overdue",
                "bills.due",
                "bank.lines",
                "vat.quarter",
                "payroll.month",
                "bank.feeds",
                "expenses.approve",
                "month.revaluation",
                "month.close",
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
                    "name": f"Invented-{index}-{row:02d}",
                    "modified": f"2026-10-{index + row + 1:02d} 00:00:00",
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
                {"name": f"Invented-{index}-{row:02d}"} for row in range(assistant.TOP)
            ]
        drafts = entry(self.collect(), "drafts")
        self.assertEqual(drafts["count"], 12 * len(assistant.DRAFT_DOCTYPES))
        self.assertEqual(len(drafts["top"]), assistant.TOP)

    def test_drafts_skip_unreadable_doctypes_at_count_and_row_reads(self):
        readable, count_denied, rows_denied = assistant.DRAFT_DOCTYPES[:3]
        FRAPPE.counts.update({readable: 2, count_denied: 5, rows_denied: 7})
        FRAPPE.rows[readable] = [
            {"name": "Invented-Readable-1", "modified": "2026-10-17"},
            {"name": "Invented-Readable-2", "modified": "2026-10-16"},
        ]
        FRAPPE.rows[rows_denied] = [
            {"name": "Invented-Rows-Denied", "modified": "2026-10-18"}
        ]
        FRAPPE.denied.add(count_denied)
        FRAPPE.denied_rows.add(rows_denied)
        drafts = entry(self.collect(), "drafts")
        self.assertEqual(drafts["count"], 2)
        self.assertEqual(drafts["by_doctype"][readable], 2)
        self.assertNotIn(count_denied, drafts["by_doctype"])
        self.assertNotIn(rows_denied, drafts["by_doctype"])
        self.assertEqual(
            [row["name"] for row in drafts["top"]],
            ["Invented-Readable-1", "Invented-Readable-2"],
        )

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
        overdue = entry(self.collect(), "sales.overdue")
        self.assertEqual(overdue["count"], 1)
        filters = last_calls("Sales Invoice")[-2]["filters"]
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters["outstanding_amount"], [">", 0])
        self.assertEqual(filters["due_date"], ["<", TODAY])

    def test_bills_due_reach_fourteen_days_ahead(self):
        FRAPPE.counts["Purchase Invoice"] = 1
        FRAPPE.rows["Purchase Invoice"] = [
            {
                "name": "PINV-2026-0007",
                "supplier": "Invented Supplier GmbH",
                "due_date": "2026-10-30",
                "outstanding_amount": 96.4,
            }
        ]
        due = entry(self.collect(), "bills.due")
        self.assertEqual(due["count"], 1)
        self.assertEqual(due["by_doctype"], {"Purchase Invoice": 1})
        filters = last_calls("Purchase Invoice")[-2]["filters"]
        self.assertEqual(filters["due_date"], ["<=", date(2026, 11, 1)])
        self.assertEqual(filters["outstanding_amount"], [">", 0])
        self.assertEqual(due["top"][0]["doctype"], "Purchase Invoice")
        self.assertEqual(due["top"][0]["date"], "2026-10-30")

    def test_bills_due_add_the_unpaid_expense_claims_when_hrms_is_there(self):
        FRAPPE.tables.add("Expense Claim")
        FRAPPE.counts = {"Purchase Invoice": 1, "Expense Claim": 1}
        FRAPPE.rows["Purchase Invoice"] = [
            {
                "name": "PINV-2026-0007",
                "supplier": "Invented Supplier GmbH",
                "due_date": "2026-10-30",
                "outstanding_amount": 96.4,
            }
        ]
        FRAPPE.rows["Expense Claim"] = [
            {
                "name": "EXP-2026-0004",
                "posting_date": "2026-10-12",
                "total_claimed_amount": 74.5,
            }
        ]
        due = entry(self.collect(), "bills.due")
        self.assertEqual(due["count"], 2)
        self.assertEqual(due["by_doctype"], {"Purchase Invoice": 1, "Expense Claim": 1})
        # the count read of bills.due: the aggregate read filtered by Unpaid status (drafts and
        # expenses.approve count Expense Claims too, by other filters)
        claim_call = next(
            kwargs
            for kwargs in last_calls("Expense Claim")
            if kwargs.get("fields") == [COUNT]
            and "status" in (kwargs.get("filters") or {})
        )
        self.assertEqual(claim_call["filters"], {"docstatus": 1, "status": "Unpaid"})
        self.assertNotIn("employee", claim_call["fields"])
        # the rows are the soonest first, whoever owes: the claim of the 12th before the bill of the 30th
        self.assertEqual(
            [row["doctype"] for row in due["top"]],
            ["Expense Claim", "Purchase Invoice"],
        )

    def test_bills_due_without_hrms_count_the_purchase_invoices_only(self):
        FRAPPE.counts["Purchase Invoice"] = 2
        due = entry(self.collect(), "bills.due")
        self.assertEqual(due["count"], 2)
        self.assertEqual(last_calls("Expense Claim"), [])

    def test_expenses_approve_list_the_claims_in_approval(self):
        FRAPPE.tables.add("Expense Claim")
        FRAPPE.counts["Expense Claim"] = 2
        FRAPPE.rows["Expense Claim"] = [
            {
                "name": "EXP-2026-0004",
                "approval_status": "Draft",
                "total_claimed_amount": 74.5,
                "posting_date": "2026-10-12",
            },
            {
                "name": "EXP-2026-0005",
                "approval_status": "Pending",
                "total_claimed_amount": 180.0,
                "posting_date": "2026-10-14",
            },
        ]
        approve = entry(self.collect(), "expenses.approve")
        self.assertEqual(approve["count"], 2)
        self.assertEqual(approve["top"][0]["name"], "EXP-2026-0004")
        call = last_calls("Expense Claim")[-2]
        self.assertEqual(call["filters"]["docstatus"], 0)
        self.assertEqual(
            call["filters"]["approval_status"], ["in", ["Draft", "Pending"]]
        )
        self.assertNotIn("employee", call["fields"])

    def test_expenses_approve_say_hrms_is_missing(self):
        approve = entry(self.collect(), "expenses.approve")
        self.assertEqual(approve["count"], 0)
        self.assertEqual(approve["top"], [])
        self.assertIn("HRMS", approve["note"])
        self.assertEqual(last_calls("Expense Claim"), [])

    def test_month_revaluation_lists_the_drafts(self):
        FRAPPE.counts["Exchange Rate Revaluation"] = 1
        FRAPPE.rows["Exchange Rate Revaluation"] = [
            {
                "name": "ERR-2026-0009",
                "company": "Invented Company AG",
                "posting_date": "2026-09-30",
            }
        ]
        reval = entry(self.collect(), "month.revaluation")
        self.assertEqual(reval["count"], 1)
        self.assertEqual(reval["top"][0]["name"], "ERR-2026-0009")
        self.assertEqual(
            last_calls("Exchange Rate Revaluation")[0]["filters"], {"docstatus": 0}
        )

    def test_month_close_counts_the_last_full_month(self):
        def bank_count(kwargs):
            filters = [tuple(row) for row in kwargs["filters"] if isinstance(row, list)]
            if ("date", ">=", date(2026, 9, 1)) in filters and (
                "date",
                "<=",
                date(2026, 9, 30),
            ) in filters:
                return 3
            return 12

        FRAPPE.count_of["Bank Transaction"] = bank_count
        FRAPPE.tables.update({"Payment Proposal", "Expense Claim"})
        FRAPPE.counts = {
            "Sales Invoice": 1,
            "Purchase Invoice": 0,
            "Payment Entry": 2,
            "Journal Entry": 0,
            "Payment Proposal": 1,
            "Expense Claim": 0,
            "Salary Slip": 1,
            "Exchange Rate Revaluation": 1,
        }
        FRAPPE.count_of["Exchange Rate Revaluation"] = lambda kwargs: (
            2
            if kwargs["filters"]
            == [
                ["docstatus", "=", 0],
                ["posting_date", ">=", date(2026, 9, 1)],
                ["posting_date", "<=", date(2026, 9, 30)],
            ]
            else 1
        )
        close = entry(self.collect(), "month.close")
        self.assertEqual(close["month"], "2026-09")
        self.assertEqual(close["lines_unreconciled"], 3)
        self.assertEqual(
            close["drafts"],
            {
                "Sales Invoice": 1,
                "Purchase Invoice": 0,
                "Payment Entry": 2,
                "Journal Entry": 0,
                "Payment Proposal": 1,
                "Expense Claim": 0,
                "Salary Slip": 1,
                "Exchange Rate Revaluation": 2,
            },
        )
        self.assertEqual(close["count"], 10)
        self.assertTrue(close["revaluation_submitted"])
        bank_filters = [
            call["filters"]
            for doctype, call in FRAPPE.lists
            if doctype == "Bank Transaction"
        ]
        self.assertEqual(bank_filters[0][0], ["docstatus", "=", 1])

    def test_month_close_reports_a_missing_revaluation(self):
        FRAPPE.counts["Bank Transaction"] = 0
        FRAPPE.counts["Exchange Rate Revaluation"] = 0
        close = entry(self.collect(), "month.close")
        self.assertFalse(close["revaluation_submitted"])
        self.assertEqual(close["count"], 0)

    def test_month_close_skips_a_draft_doctype_the_user_may_not_read(self):
        FRAPPE.counts["Bank Transaction"] = 0
        FRAPPE.counts["Sales Invoice"] = 1
        FRAPPE.denied.add("Sales Invoice")
        close = entry(self.collect(), "month.close")
        self.assertNotIn("Sales Invoice", close["drafts"])
        self.assertEqual(close["count"], 0)

    def test_bank_lines_decide_the_recent_lines_with_the_match_job(self):
        FRAPPE.readable.update(set(assistant.BANK_LINE_DOCTYPES))

        def bank_count(kwargs):
            filters = [tuple(row) for row in kwargs["filters"] if isinstance(row, list)]
            if ("date", ">=", date(2026, 9, 1)) in filters:
                return 2
            if ("date", "<", date(2026, 9, 1)) in filters:
                return 3
            return 5

        def bank_groups(kwargs):
            filters = [tuple(row) for row in kwargs["filters"] if isinstance(row, list)]
            if ("date", "<", date(2026, 9, 1)) in filters:
                return [{"bank_account": "Invented Bank CHF", "n": 3}]
            return [{"bank_account": "Invented Bank CHF", "n": 2}]

        FRAPPE.count_of["Bank Transaction"] = bank_count
        FRAPPE.groups_of["Bank Transaction"] = bank_groups
        FRAPPE.rows["Bank Transaction"] = [
            {
                "name": "BT-2026-0002",
                "date": "2026-10-05",
                "deposit": 100.0,
                "withdrawal": 0.0,
                "description": "Invented Customer AG",
                "reference_number": creditor_reference("INV-2026-0042"),
                "transaction_id": None,
                "end_to_end_id": None,
                "currency": "CHF",
                "company": "Invented Company AG",
                "bank_account": "Invented Bank CHF",
            },
            {
                "name": "BT-2026-0001",
                "date": "2026-09-20",
                "deposit": 0.0,
                "withdrawal": 50.0,
                "description": "",
                "reference_number": "",
                "transaction_id": None,
                "end_to_end_id": None,
                "currency": "CHF",
                "company": "Invented Company AG",
                "bank_account": "Invented Bank CHF",
            },
        ]
        FRAPPE.values[("Bank Account", "Invented Bank CHF")] = (
            "Invented GL CHF",
            "Invented Company AG",
        )
        FRAPPE.values[("Account", "Invented GL CHF")] = "CHF"
        JOB.docs = [
            {
                "doctype": "Sales Invoice",
                "name": "INV-2026-0042",
                "party_name": "Invented Customer AG",
                "direction": "in",
                "amount": 100.0,
                "currency": "CHF",
                "date": "2026-10-04",
                "reference": bank_match.key(creditor_reference("INV-2026-0042")),
            }
        ]
        bank = entry(self.collect(), "bank.lines")
        bank_filters = [
            kwargs["filters"]
            for doctype, kwargs in FRAPPE.lists
            if doctype == "Bank Transaction"
        ]
        self.assertTrue(
            all(["docstatus", "=", 1] in filters for filters in bank_filters)
        )
        self.assertEqual(bank["count"], 5)
        self.assertEqual(bank["recent"], 2)
        self.assertEqual(bank["older"], 3)
        self.assertEqual(
            bank["by_account"],
            [{"bank_account": "Invented Bank CHF", "recent": 2, "older": 3}],
        )
        self.assertEqual(
            bank["reasons"],
            {"several": 0, "shared": 0, "not_booked": 0, "matched": 1, "none": 1},
        )
        self.assertEqual(
            [row["name"] for row in bank["top"]], ["BT-2026-0002", "BT-2026-0001"]
        )
        self.assertEqual(bank["top"][0]["reason"], "matched")
        self.assertEqual(bank["top"][0]["amount"], 100.0)
        self.assertEqual(bank["top"][1]["reason"], "none")
        # the job's own readers ran, with the account's company and currency
        self.assertIn(("documents", "Invented Company AG", "CHF"), JOB.calls)
        self.assertIn(
            ("payments", "Invented Company AG", "CHF", "Invented GL CHF"), JOB.calls
        )
        self.assertIn(("rules", "Invented Company AG"), JOB.calls)
        self.assertEqual(JOB.users, [USER, USER, USER])
        line_calls = [
            kwargs
            for called, kwargs in FRAPPE.lists
            if called == "Bank Transaction"
            and "limit_page_length" in kwargs
            and kwargs.get("fields")
            != [COUNT]  # the count reads are capped too, but are not rows
        ]
        self.assertEqual(line_calls[0]["limit_page_length"], assistant.TOP)
        self.assertEqual(line_calls[0]["order_by"], "date desc")

    def test_bank_lines_count_every_reason_the_job_knows(self):
        FRAPPE.readable.update(set(assistant.BANK_LINE_DOCTYPES))

        def bank_count(kwargs):
            filters = [tuple(row) for row in kwargs["filters"] if isinstance(row, list)]
            if ("date", "<", date(2026, 9, 1)) in filters:
                return 0
            return 5

        FRAPPE.count_of["Bank Transaction"] = bank_count

        def bank_groups(kwargs):
            filters = [tuple(row) for row in kwargs["filters"] if isinstance(row, list)]
            if ("date", ">=", date(2026, 9, 1)) in filters:
                return [{"bank_account": "Invented Bank CHF", "n": 5}]
            return []

        FRAPPE.groups_of["Bank Transaction"] = bank_groups
        FRAPPE.rows["Bank Transaction"] = [
            self.bank_line("BT-2026-0002", "2026-10-05", 100.0, reference="RF185390"),
            self.bank_line("BT-2026-0003", "2026-10-05", 100.0, reference="RF185390"),
            self.bank_line(
                "BT-2026-0004",
                "2026-09-25",
                60.0,
                description="Invented Customer AG invoice",
            ),
            self.bank_line(
                "BT-2026-0005",
                "2026-09-22",
                -30.0,
                description="invented fee bank charge",
            ),
            self.bank_line("BT-2026-0006", "2026-09-20", -40.0),
        ]
        FRAPPE.values[("Bank Account", "Invented Bank CHF")] = (
            "Invented GL CHF",
            "Invented Company AG",
        )
        FRAPPE.values[("Account", "Invented GL CHF")] = "CHF"
        JOB.docs = [
            self.job_doc("INV-2026-0042", "in", 100.0, reference="RF185390"),
            self.job_doc(
                "INV-2026-0060",
                "in",
                60.0,
                party="Invented Customer AG",
                day="2026-09-24",
            ),
            self.job_doc(
                "INV-2026-0061",
                "in",
                60.0,
                party="Invented Customer AG",
                day="2026-09-26",
            ),
        ]
        JOB.rules = [
            {
                "name": "RULE-UNBOOKED",
                "priority": 0,
                "company": "Invented Company AG",
                "transaction_type": "Withdrawal",
                "min_amount": None,
                "max_amount": None,
                "classify_as": "Bank Entry",
                "bank_entry_type": "Split Account",
                "account": None,
                "description_rules": [{"check": "Contains", "value": "invented fee"}],
            }
        ]
        bank = entry(self.collect(), "bank.lines")
        self.assertEqual(bank["recent"], 5)
        self.assertEqual(bank["older"], 0)
        # two lines claim the same bill (shared), one names two bills (several),
        # one a rule this job does not book (not booked), one nothing at all (none)
        self.assertEqual(
            bank["reasons"],
            {"several": 1, "shared": 2, "not_booked": 1, "matched": 0, "none": 1},
        )
        self.assertEqual(
            [row["reason"] for row in bank["top"]],
            ["shared", "shared", "several", "not_booked", "none"],
        )

    def test_bank_lines_report_nothing_when_a_match_read_is_not_permitted(self):
        FRAPPE.readable = set()
        FRAPPE.counts["Bank Transaction"] = 5
        bank = entry(self.collect(), "bank.lines")
        self.assertIsNone(bank["count"])
        self.assertIn("note", bank)
        self.assertEqual(JOB.calls, [])

    def test_bank_lines_keeps_all_grouped_accounts(self):
        FRAPPE.readable.update(set(assistant.BANK_LINE_DOCTYPES))
        FRAPPE.counts["Bank Transaction"] = 0
        FRAPPE.groups_of["Bank Transaction"] = lambda kwargs: [
            {"bank_account": f"Invented Bank {index:02d}", "n": 1}
            for index in range(12)
        ]
        bank = entry(self.collect(), "bank.lines")
        self.assertEqual(len(bank["by_account"]), 12)
        self.assertEqual(bank["by_account"][-1]["bank_account"], "Invented Bank 11")

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
        vat = entry(self.collect(), "vat.quarter")
        self.assertEqual(vat["count"], 12)
        self.assertFalse(vat["missing"])
        self.assertEqual(vat["period"], "2026-07-01 to 2026-09-30")
        filters = last_calls("VAT Declaration")[-1]["filters"]
        self.assertEqual(filters["start_date"], ["<=", date(2026, 7, 1)])
        self.assertEqual(filters["end_date"], [">=", date(2026, 9, 30)])

    def test_a_missing_declaration_is_reported_as_missing(self):
        vat = entry(self.collect(), "vat.quarter")
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
        FRAPPE.rows["Salary Slip"] = [{"name": "SAL-2026-0003", "docstatus": 0}]
        payroll = entry(self.collect(), "payroll.month")
        self.assertEqual(payroll["month"], "2026-10")
        self.assertEqual(payroll["submitted"], 2)
        self.assertEqual(payroll["drafts"], 1)
        self.assertEqual(payroll["count"], 3)
        self.assertIn(
            ["name", "docstatus"],
            [call["fields"] for call in last_calls("Salary Slip")],
        )
        # no read of the slips ever asks for the employee (the review of erp-r6zb)
        for call in last_calls("Salary Slip"):
            self.assertNotIn("employee", call["fields"])
        self.assertNotIn("employee", payroll["top"][0])
        bounds = last_calls("Salary Slip")[1]["filters"]
        self.assertIn(["start_date", ">=", date(2026, 10, 1)], bounds)
        self.assertIn(["start_date", "<=", date(2026, 10, 31)], bounds)
        row_read = next(
            kwargs
            for kwargs in last_calls("Salary Slip")
            if kwargs.get("fields") == ["name", "docstatus"]
        )
        self.assertEqual(row_read["filters"][0], ["docstatus", "<", 2])

    def test_bank_feeds_flag_the_off_and_the_stale(self):
        FRAPPE.singles = {
            "Wise Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 08:00:00",  # four hours before the stub clock (12:00)
            },
            "PayPal Settings": {"sync_enabled": 0, "last_sync": None},
        }
        FRAPPE.counts["Bank Statement Upload"] = 1  # the upload month has its own tests
        feeds = entry(self.collect(), "bank.feeds")
        self.assertEqual(feeds["count"], 2)
        wise, paypal = feeds["top"]
        self.assertEqual(wise["feed"], "Wise")
        self.assertTrue(wise["sync_enabled"])
        self.assertEqual(wise["hours_since"], 4.0)
        self.assertEqual(wise["flag"], "last sync more than 3 h ago")
        self.assertFalse(paypal["sync_enabled"])
        self.assertEqual(paypal["flag"], "sync is off")

    def test_bank_feeds_leave_a_fresh_sync_unflagged(self):
        FRAPPE.singles = {
            "Wise Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:30:00",
            },
            "PayPal Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:00:00",
            },
        }
        FRAPPE.counts["Bank Statement Upload"] = 1
        feeds = entry(self.collect(), "bank.feeds")
        self.assertEqual(feeds["count"], 0)
        self.assertEqual([row["flag"] for row in feeds["top"]], [None, None])

    def test_bank_feeds_report_the_latest_upload_and_its_month(self):
        FRAPPE.singles = {
            "Wise Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:30:00",
            },
            "PayPal Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:30:00",
            },
        }
        FRAPPE.counts["Bank Statement Upload"] = 2
        FRAPPE.rows["Bank Statement Upload"] = [
            {"name": "BSU-2026-0004", "imported_on": "2026-10-02 09:00:00"}
        ]
        feeds = entry(self.collect(), "bank.feeds")
        self.assertEqual(feeds["count"], 0)
        self.assertEqual(feeds["latest_upload"], "BSU-2026-0004")
        self.assertEqual(feeds["upload_month"], "2026-10")
        self.assertFalse(feeds["upload_month_missing"])

    def test_bank_feeds_flag_a_last_full_month_without_upload(self):
        FRAPPE.singles = {
            "Wise Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:30:00",
            },
            "PayPal Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 11:30:00",
            },
        }
        FRAPPE.counts["Bank Statement Upload"] = 0
        feeds = entry(self.collect(), "bank.feeds")
        self.assertEqual(feeds["count"], 1)
        self.assertTrue(feeds["upload_month_missing"])
        self.assertIsNone(feeds["latest_upload"])

    def test_the_upload_month_is_counted_between_the_month_bounds(self):
        # imported_on is a Datetime field: between extends the upper bound to the end of that day,
        # while <= on a date stops at its midnight and would miss an import from the last day
        FRAPPE.counts["Bank Statement Upload"] = 1
        feeds = entry(self.collect(), "bank.feeds")
        upload_call = next(
            kwargs
            for kwargs in last_calls("Bank Statement Upload")
            if kwargs.get("fields") == [COUNT]
        )
        self.assertEqual(
            upload_call["filters"],
            [["imported_on", "between", [date(2026, 9, 1), date(2026, 9, 30)]]],
        )
        self.assertFalse(feeds["upload_month_missing"])

    def test_a_feed_the_user_cannot_read_is_skipped(self):
        FRAPPE.readable = set()
        FRAPPE.singles = {
            "Wise Settings": {
                "sync_enabled": 1,
                "last_sync": "2026-10-18 08:00:00",
            }
        }
        # the upload month has its own test: here an existing upload keeps it quiet, so only the
        # unreadable feed is under test
        FRAPPE.counts["Bank Statement Upload"] = 1
        feeds = entry(self.collect(), "bank.feeds")
        self.assertEqual(feeds["count"], 0)
        self.assertEqual(feeds["top"], [])

    def test_a_doctype_the_user_may_not_read_is_left_out_not_fatal(self):
        FRAPPE.denied.add("Salary Slip")
        result = self.collect()
        payroll = entry(result, "payroll.month")
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
