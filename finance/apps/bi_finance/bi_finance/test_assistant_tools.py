"""The model proposes actions (erp-wcf3, part 2b): the tools, the tool calls, the cards and the whitelisted endpoints.

Invented users, documents and round amounts only. frappe is the stub of test_assistant_actions (rows in memory) with the
few calls assistant.py adds, and the HTTP client gets the fake opener of test_assistant, so the whole way runs on the
host in the light gate (.yardr/check-host.sh):

    python3 -m unittest bi_finance.test_assistant_tools

Pinned: the tools are built from the registry and from nothing else; they are not offered with the kill switch off; one
tool call becomes exactly one AI Action, an unknown or never type and a call failing its schema included (refused and
logged); the provider settings are on every request and the key is in the header alone; level 1 is approved by the one
who asked and by nobody else; the endpoints take an AI Action name and no user.
"""

import contextlib
import inspect
import io
import json
import logging
import types
import unittest
from unittest import mock

from bi_finance import assistant_levels as levels
from bi_finance.test_assistant import KEY, FakeOpener, assistant, assistant_client
from bi_finance.test_assistant_actions import OTHER, TODAY, USER, Base, Doc

COMMENT_ARGS = {
    "reference_doctype": "Sales Invoice",
    "reference_name": "SI-1",
    "content": "Reminder sent on the phone.",
}


def reply(*calls, content=None):
    """An OpenRouter reply of tool calls: each call is (name, arguments dict or the raw text of them)."""
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": f"call_{n}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": (
                                    arguments
                                    if isinstance(arguments, str)
                                    else json.dumps(arguments)
                                ),
                            },
                        }
                        for n, (name, arguments) in enumerate(calls)
                    ],
                }
            }
        ]
    }


class AskBase(Base):
    """The action core's stub, widened by what assistant.ask touches, with the model replaced by a fake opener."""

    def setUp(self):
        super().setUp()
        frappe = self.frappe
        frappe._ = lambda text: text
        frappe.thrown = []

        def throw(message):
            frappe.thrown.append(message)
            raise frappe.ValidationError(message)

        frappe.throw = throw
        frappe.settings.enabled = 1
        frappe.settings.model = "invented/model"
        frappe.settings.max_tokens_per_answer = 500
        frappe.settings.get_password = lambda fieldname, raise_exception=False: KEY
        frappe.rows.setdefault("Sales Invoice", {})["SI-1"] = Doc(
            frappe, {"doctype": "Sales Invoice", "name": "SI-1"}
        )
        patcher = mock.patch.object(assistant, "frappe", frappe)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(assistant, "collect", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opener = FakeOpener()

    def ask(self, *calls, content=None, message="Please remind them."):
        """assistant.ask with the real client, the fake opener answering with the tool calls given."""
        if content is None and not calls:
            content = json.dumps({"text": "Nothing to do.", "items": []})
        self.opener.answer = reply(*calls, content=content)
        real = assistant_client.answer
        with mock.patch.object(
            assistant_client,
            "answer",
            side_effect=lambda **kwargs: real(opener=self.opener, **kwargs),
        ):
            return assistant.ask(message)

    def body(self):
        return json.loads(self.opener.requests[-1].data)


class Tools(AskBase):
    def test_one_function_per_registered_type_and_nothing_else(self):
        tools = assistant.build_tools()
        self.assertEqual(
            sorted(tool["function"]["name"] for tool in tools),
            sorted(assistant.assistant_actions.ACTIONS),
        )
        self.assertTrue(all(tool["type"] == "function" for tool in tools))
        # the stub types registered by the base show up by themselves, as the ledger types will
        self.assertIn("stub_book", {tool["function"]["name"] for tool in tools})

    def test_a_never_type_is_not_offered(self):
        offered = {tool["function"]["name"] for tool in assistant.build_tools()}
        self.assertEqual(
            offered & set(assistant.assistant_actions.NEVER_ACTIONS), set()
        )

    def test_an_empty_registry_offers_nothing(self):
        self.assertEqual(assistant.build_tools(registry={}), [])

    def test_each_tool_takes_the_types_schema_plus_task_and_confidence(self):
        for tool in assistant.build_tools():
            name = tool["function"]["name"]
            spec = assistant.assistant_actions.ACTIONS[name]
            parameters = tool["function"]["parameters"]
            self.assertLessEqual(
                set(spec.schema["properties"]), set(parameters["properties"])
            )
            self.assertEqual(
                parameters["required"],
                list(spec.schema.get("required", [])) + ["task", "confidence"],
            )
            self.assertEqual(
                parameters["properties"]["task"]["enum"],
                list(assistant_client.TASK_IDS),
            )
            confidence = parameters["properties"]["confidence"]
            self.assertEqual(
                (confidence["type"], confidence["minimum"], confidence["maximum"]),
                ("number", 0, 1),
            )

    def test_the_registrys_own_schema_is_left_as_registered(self):
        assistant.build_tools()
        for spec in assistant.assistant_actions.ACTIONS.values():
            self.assertNotIn("task", spec.schema["properties"])
            self.assertNotIn("confidence", spec.schema["properties"])
            self.assertNotIn("confidence", spec.schema.get("required", []))

    def test_tools_are_absent_when_the_kill_switch_is_off(self):
        self.frappe.settings.actions_enabled = 0
        self.ask()
        self.assertNotIn("tools", self.body())
        self.assertNotIn("tool_choice", self.body())
        self.assertIn("You can only read", self.body()["messages"][0]["content"])

    def test_a_call_the_model_makes_anyway_is_not_proposed_with_the_switch_off(self):
        self.frappe.settings.actions_enabled = 0
        result = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)),
            content=json.dumps({"text": "Done.", "items": []}),
        )
        self.assertEqual(result["actions"], [])
        self.assertEqual(self.rows(), {})

    def test_tools_are_sent_with_the_switch_on(self):
        self.ask(("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)))
        body = self.body()
        self.assertEqual(body["tools"], assistant.build_tools())
        self.assertEqual(body["tool_choice"], "auto")
        system = body["messages"][0]["content"]
        self.assertNotIn("You can only read", system)
        self.assertIn("task", system)
        self.assertIn("confidence", system)
        for task in assistant_client.TASK_IDS:
            self.assertIn(task, system)


class Request(AskBase):
    def test_the_provider_settings_are_on_the_request_with_and_without_tools(self):
        self.ask(("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)))
        with_tools = self.body()["provider"]
        self.frappe.settings.actions_enabled = 0
        self.ask()
        without = self.body()["provider"]
        expected = {"allow_fallbacks": False, "zdr": True, "data_collection": "deny"}
        self.assertEqual((with_tools, without), (expected, expected))

    def test_the_key_is_in_the_header_alone_and_in_no_response_row_or_log(self):
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = self.ask(
                ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)),
                ("transfer_money", {"amount": "10"}),
            )
        request = self.opener.requests[-1]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn(KEY.encode(), request.data)
        stored = json.dumps([row.__dict__ for row in self.rows().values()], default=str)
        for seen in (
            json.dumps(result),
            stored,
            logs.getvalue(),
            out.getvalue(),
            err.getvalue(),
        ):
            self.assertNotIn(KEY, seen)

    def test_the_banner_adds_the_dry_run_rule_to_the_action_prompt(self):
        self.frappe.settings.ledger_actions_from = "2027-06-01"
        self.ask()
        system = self.body()["messages"][0]["content"]
        self.assertIn("bexio leads until 1.6.2027", system)
        self.assertIn("dry run", system)


class ToolCalls(AskBase):
    def test_a_tool_call_becomes_exactly_one_ai_action(self):
        result = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="sales.overdue", confidence=0.95))
        )
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(result["actions"]), 1)
        card = result["actions"][0]
        row = self.rows()[card["name"]]
        self.assertEqual(
            (row.requested_by, row.task, row.action_type, row.model_score),
            (USER, "sales.overdue", "add_comment", 0.95),
        )
        # task and confidence are the proposal's, not the action's arguments
        self.assertEqual(json.loads(row.payload), COMMENT_ARGS)

    def test_the_card_carries_what_the_page_shows(self):
        card = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=0.95))
        )["actions"][0]
        self.assertEqual(
            (card["type"], card["task"], card["level"], card["state"]),
            ("add_comment", "drafts", "0", "auto-done"),
        )
        self.assertEqual(
            (card["target_doctype"], card["target_name"]), ("Sales Invoice", "SI-1")
        )
        self.assertEqual(card["confidence"], 0.95)
        self.assertTrue(card["ok"])
        self.assertTrue(card["undoable"])
        self.assertFalse(card["dry_run"])
        self.assertEqual(card["form"], "/app/ai-action/AI%20Action-1")
        self.assertIn(
            {
                "check": "target readable",
                "passed": True,
                "detail": "Sales Invoice SI-1 can be read",
            },
            card["checks"],
        )

    def test_two_calls_are_two_actions(self):
        result = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)),
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)),
        )
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_an_unknown_type_is_refused_and_logged(self):
        card = self.ask(
            ("transfer_money", {"amount": "10", "task": "drafts", "confidence": 1})
        )["actions"][0]
        self.assertEqual(
            (card["state"], card["level"], card["ok"]), ("rejected", "never", False)
        )
        self.assertIn("not an action", card["message"])
        row = self.rows()[card["name"]]
        self.assertEqual((row.action_type, row.state), ("transfer_money", "rejected"))

    def test_a_never_type_is_refused_and_logged_even_if_the_model_names_it(self):
        card = self.ask(("delete_doc", {"task": "drafts", "confidence": 1}))["actions"][
            0
        ]
        self.assertEqual((card["state"], card["level"]), ("rejected", "never"))
        self.assertIn("never", card["message"])
        self.assertEqual(len(self.rows()), 1)

    def test_a_call_failing_the_schema_is_refused_and_logged(self):
        card = self.ask(
            (
                "add_comment",
                {
                    "reference_doctype": "Sales Invoice",
                    "task": "drafts",
                    "confidence": 1,
                },
            )
        )["actions"][0]
        self.assertEqual((card["state"], card["level"]), ("rejected", "never"))
        failed = [row for row in card["checks"] if not row["passed"]]
        self.assertEqual([row["check"] for row in failed], ["arguments"])
        self.assertEqual(len(self.rows()), 1)

    def test_arguments_that_are_no_json_object_are_refused_and_logged(self):
        for raw in ("{not json", "[1, 2]", '"text"'):
            card = self.ask(("add_comment", raw))["actions"][-1]
            self.assertEqual((card["state"], card["level"]), ("rejected", "never"), raw)
        self.assertEqual(len(self.rows()), 3)

    def test_a_missing_confidence_is_no_confidence(self):
        card = self.ask(("add_comment", dict(COMMENT_ARGS, task="drafts")))["actions"][
            0
        ]
        # a level 0 type without a score does not apply on its own: it waits for a click
        self.assertEqual(
            (card["level"], card["state"], card["confidence"]), ("1", "proposed", 0.0)
        )

    def test_a_low_confidence_moves_a_level_0_type_to_a_click(self):
        card = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=0.5))
        )["actions"][0]
        self.assertEqual((card["level"], card["state"]), ("1", "proposed"))

    def test_more_calls_than_the_cap_are_not_all_proposed(self):
        calls = [("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1))] * 15
        result = self.ask(*calls)
        self.assertEqual(len(result["actions"]), assistant_client.MAX_CALLS)
        self.assertEqual(len(self.rows()), assistant_client.MAX_CALLS)

    def test_a_reply_of_calls_alone_gets_a_text(self):
        result = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1))
        )
        self.assertIn("1 action", result["text"])

    def test_the_models_text_stays_when_it_gave_one(self):
        result = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1)),
            content=json.dumps({"text": "I noted it.", "items": []}),
        )
        self.assertEqual(result["text"], "I noted it.")

    def test_no_calls_no_cards(self):
        self.opener.answer = {
            "choices": [
                {"message": {"content": json.dumps({"text": "Nothing.", "items": []})}}
            ]
        }
        real = assistant_client.answer
        with mock.patch.object(
            assistant_client,
            "answer",
            side_effect=lambda **kw: real(opener=self.opener, **kw),
        ):
            result = assistant.ask("What is open?")
        self.assertEqual(result["actions"], [])


class LevelOne(AskBase):
    def card(self, name):
        return assistant._card({"name": name, "ok": True})

    def test_a_level_1_card_waits_for_the_one_who_asked(self):
        card = self.ask(
            (
                "stub_book",
                {
                    "amount": "250.00",
                    "date": "2027-03-01",
                    "task": "bank.lines",
                    "confidence": 1,
                },
            )
        )["actions"][0]
        self.assertEqual((card["level"], card["state"]), ("1", "proposed"))
        self.assertFalse(card["dry_run"])

    def test_before_the_ledger_date_a_ledger_action_is_level_2_with_the_default_ladder(
        self,
    ):
        # 2a's ledger date check caps the confidence at 0.5, below the one-click threshold: a second step, shown as such
        card = self.ask(
            (
                "stub_book",
                {
                    "amount": "250.00",
                    "date": "2026-12-01",
                    "task": "bank.lines",
                    "confidence": 1,
                },
            )
        )["actions"][0]
        self.assertEqual(
            (card["level"], card["state"], card["dry_run"]), ("2", "proposed", True)
        )

    def test_before_the_ledger_date_the_card_says_dry_run(self):
        self.frappe.settings.one_click_threshold = (
            0.5  # the cap of the ledger date check lets level 1 through
        )
        card = self.ask(
            (
                "stub_book",
                {
                    "amount": "250.00",
                    "date": "2026-12-01",
                    "task": "bank.lines",
                    "confidence": 1,
                },
            )
        )["actions"][0]
        self.assertTrue(card["dry_run"])
        approved = assistant.approve_action(card["name"])
        self.assertEqual(approved["state"], "approved-dry-run")
        self.assertTrue(approved["ok"])
        self.assertIn("would book", approved["dry_run_result"])
        self.assertEqual(self.one_click_applied[-1][1], True)  # applied as a dry run

    def test_the_one_who_asked_approves_and_the_action_applies(self):
        name = self.book()["name"]
        approved = assistant.approve_action(name)
        self.assertEqual((approved["ok"], approved["state"]), (True, "approved"))
        self.assertEqual(self.one_click_applied[-1][1], False)

    def test_another_user_cannot_approve_level_1(self):
        name = self.book()["name"]
        self.switch(OTHER)
        refused = assistant.approve_action(name)
        self.assertEqual((refused["ok"], refused["state"]), (False, "proposed"))
        self.assertIn("Only the user who asked", refused["message"])
        self.assertEqual(self.one_click_applied, [])

    def test_another_user_cannot_reject_it_either(self):
        name = self.book()["name"]
        self.switch(OTHER)
        self.frappe.roles[OTHER] = []
        refused = assistant.reject_action(name)
        self.assertEqual((refused["ok"], refused["state"]), (False, "proposed"))

    def test_reject_by_the_one_who_asked(self):
        name = self.book()["name"]
        rejected = assistant.reject_action(name)
        self.assertEqual((rejected["ok"], rejected["state"]), (True, "rejected"))
        self.assertEqual(self.one_click_applied, [])

    def test_a_level_2_card_is_not_approved_from_the_page(self):
        name = self.book(action="stub_submit")["name"]
        card = self.card(name)
        self.assertEqual((card["level"], card["state"]), ("2", "proposed"))
        self.assertEqual(card["form"], "/app/ai-action/" + name.replace(" ", "%20"))
        refused = assistant.approve_action(name)
        self.assertEqual((refused["ok"], refused["state"]), (False, "proposed"))
        self.assertEqual(self.always_applied, [])

    def test_undo_takes_back_a_level_0_action(self):
        card = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1))
        )["actions"][0]
        undone = assistant.undo_action(card["name"])
        self.assertEqual((undone["ok"], undone["state"]), (True, "undone"))
        self.assertEqual(len(self.frappe.deleted), 1)

    def test_undo_by_another_user_is_refused(self):
        card = self.ask(
            ("add_comment", dict(COMMENT_ARGS, task="drafts", confidence=1))
        )["actions"][0]
        self.switch(OTHER)
        self.frappe.roles[OTHER] = []
        refused = assistant.undo_action(card["name"])
        self.assertEqual((refused["ok"], refused["state"]), (False, "auto-done"))
        self.assertEqual(self.frappe.deleted, [])


class Endpoints(AskBase):
    def test_they_are_whitelisted_and_take_an_action_name_and_no_user(self):
        for function in (
            assistant.approve_action,
            assistant.reject_action,
            assistant.undo_action,
        ):
            self.assertTrue(function.is_whitelisted)
            self.assertEqual(list(inspect.signature(function).parameters), ["action"])

    def test_a_name_that_is_no_text_is_refused(self):
        for bad in (None, "", "   ", {"name": "x"}, ["x"]):
            with self.assertRaises(self.frappe.ValidationError):
                assistant.approve_action(bad)

    def test_the_session_user_is_the_one_asked_about(self):
        name = self.book()["name"]
        seen = []
        with mock.patch.object(
            assistant.assistant_actions,
            "approve",
            side_effect=lambda action, user: seen.append((action, user))
            or {"name": name, "ok": True},
        ):
            assistant.approve_action(name)
        self.assertEqual(seen, [(name, USER)])


class Proposals(unittest.TestCase):
    def run_collector(self, key):
        entry = {"key": key, "label": "x", "run": lambda user, today: {"count": 0}}
        return assistant._one(entry, USER, TODAY)

    def test_a_task_names_the_registered_types_it_may_use(self):
        self.assertEqual(
            self.run_collector("vat.quarter")["proposals"], ["create_vat_draft"]
        )
        self.assertEqual(
            self.run_collector("bills.due")["proposals"], ["create_payment_proposal"]
        )
        self.assertEqual(
            self.run_collector("sales.overdue")["proposals"],
            ["create_todo", "add_comment"],
        )

    def test_a_type_that_is_not_registered_yet_is_left_out(self):
        for key in ("bank.lines", "month.revaluation"):
            self.assertNotIn("proposals", self.run_collector(key))

    def test_a_task_without_a_proposal_has_none(self):
        for key in ("bank.feeds", "expenses.approve", "month.close", "payroll.month"):
            self.assertNotIn("proposals", self.run_collector(key))

    def test_the_task_ids_are_the_collector_keys(self):
        keys = {entry["key"] for entry in assistant.COLLECTORS} - {"todos"}
        self.assertEqual(keys, set(assistant_client.TASK_IDS))
        self.assertLessEqual(
            set(assistant.TASK_ACTIONS), set(assistant_client.TASK_IDS)
        )


class Client(unittest.TestCase):
    def test_arguments_are_read_as_json_and_never_raise(self):
        calls = assistant_client.tool_calls(
            reply(
                ("a", {"x": "1"}),
                ("b", "{broken"),
                ("c", "[1]"),
            )["choices"][
                0
            ]["message"]
        )
        self.assertEqual(
            calls,
            [
                {"name": "a", "arguments": {"x": "1"}},
                {"name": "b", "arguments": {}},
                {"name": "c", "arguments": {}},
            ],
        )

    def test_junk_in_the_reply_is_kept_as_an_unknown_call_not_a_crash(self):
        message = {
            "tool_calls": [
                "x",
                {"function": "y"},
                {"function": {"name": 5, "arguments": 7}},
            ]
        }
        self.assertEqual(
            assistant_client.tool_calls(message),
            [{"name": "", "arguments": {}}] * 3,
        )

    def test_no_tool_calls_field_is_no_calls(self):
        self.assertEqual(assistant_client.tool_calls({"content": "x"}), [])
        self.assertEqual(assistant_client.tool_calls({"tool_calls": "x"}), [])

    def test_the_content_may_be_null_when_tools_were_offered(self):
        answer = assistant_client.answer(
            KEY,
            "m",
            10,
            "q",
            [],
            [],
            tools=[{"type": "function"}],
            opener=FakeOpener(reply(("add_comment", {}))),
        )
        self.assertEqual((answer["text"], answer["items"]), ("", []))
        self.assertEqual(answer["calls"], [{"name": "add_comment", "arguments": {}}])

    def test_null_content_without_calls_is_still_an_error(self):
        with self.assertRaises(assistant_client.AssistantError):
            assistant_client.answer(
                KEY,
                "m",
                10,
                "q",
                [],
                [],
                tools=[{"type": "function"}],
                opener=FakeOpener({"choices": [{"message": {"content": None}}]}),
            )

    def test_calls_are_ignored_when_no_tools_were_offered(self):
        answer = assistant_client.answer(
            KEY,
            "m",
            10,
            "q",
            [],
            [],
            opener=FakeOpener(
                reply(
                    ("add_comment", {}), content=json.dumps({"text": "t", "items": []})
                )
            ),
        )
        self.assertEqual(answer["calls"], [])

    def test_the_prompt_without_tools_is_the_read_only_one(self):
        prompt = assistant_client.system_prompt([])
        self.assertIn(assistant_client.READ_ONLY_RULE, prompt)
        self.assertNotIn("task ids", prompt)


if __name__ == "__main__":
    unittest.main()
