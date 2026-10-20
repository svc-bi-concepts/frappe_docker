"""The desk assistant's action core: the registry, propose / approve / reject / undo and the AI Action rows.

Invented users, documents and round amounts only. frappe is a stub in the module itself (rows in memory, recorded
calls). The ledger effect, the ledger date and the dry run are tested here with two stub types (a one-click and an
always-approver ledger type); the four real ledger types are in test_assistant_ledger_actions. Runs on the host, in
the light gate:

    python3 -m unittest bi_finance.test_assistant_actions

Pinned: each path writes or updates one AI Action row (the refused ones too); the kill switch; never and unregistered
types; the level 1 and level 2 approval rules (another user, the API user, the typed amount, the requester when no
other AI Approver exists); a ledger action before the ledger date ends approved-dry-run and writes nothing; the five
level 0 actions with their checks and their Undo.
"""

import contextlib
import datetime
import io
import json
import os
import sys
import types
import unittest
from datetime import date
from unittest import mock

import bi_finance
from bi_finance import assistant_levels as levels

TODAY = date(2027, 2, 10)
USER = "owner@example.com"
OTHER = "second@example.com"
API = "api-agent@finance.local"


class Doc:
    """A stored row: attributes are its fields; insert and save write it to the stub's rows."""

    def __init__(self, frappe, values):
        self.__dict__["_frappe"] = frappe
        self.__dict__["name"] = values.get("name")
        self.__dict__.update(values)
        self.__dict__.setdefault("checks", [])

    def __getattr__(self, name):
        # a document field nobody set reads as None, as in frappe
        if name.startswith("__"):
            raise AttributeError(name)
        return None

    def update(self, values):
        self.__dict__.update(values)

    def insert(self, ignore_permissions=False):
        frappe = self._frappe
        self.name = (
            self.name or f"{self.doctype}-{len(frappe.rows.get(self.doctype, {})) + 1}"
        )
        frappe.rows.setdefault(self.doctype, {})[self.name] = self
        frappe.writes.append((self.doctype, "insert", self.name))
        return self

    def save(self, ignore_permissions=False):
        self._frappe.writes.append((self.doctype, "save", self.name))
        return self

    def add_comment(self, kind, text):
        return self._frappe.get_doc(
            {
                "doctype": "Comment",
                "comment_type": kind,
                "content": text,
                "reference_doctype": self.doctype,
                "reference_name": self.name,
            }
        ).insert()


class Frappe(types.ModuleType):
    class ValidationError(Exception):
        pass

    def __init__(self):
        super().__init__("frappe")
        self.session = types.SimpleNamespace(user=USER)
        self.rows = {}  # doctype -> {name: Doc}
        self.writes = []  # (doctype, verb, name), every insert and save
        self.settings = types.SimpleNamespace(
            actions_enabled=1,
            auto_level_enabled=1,
            auto_threshold=0.8,
            one_click_threshold=0.9,
            one_click_amount_limit=1000,
            auto_actions_per_day=50,
            ledger_actions_from="2027-01-01",
        )
        self.roles = {}  # user -> roles
        self.has_role = []  # (user, enabled) of the Has Role rows of AI Approver
        self.unreadable = set()
        self.matches = {}  # doctype -> callable(filters) -> list of names for get_all
        self.tables = set()
        self.auto_today = 0
        self.counts = []
        self.calls = []
        self.deleted = []
        self.locked = []  # (doctype, name, for_update) of every get_doc by name
        self.savepoints = []
        self.rolled_back = []
        self.utils = types.SimpleNamespace(getdate=lambda *a: TODAY)
        self.defaults = types.SimpleNamespace(
            get_global_default=lambda key: "Invented Co"
        )
        self.db = types.SimpleNamespace(
            exists=self._exists,
            count=self._count,
            get_value=self._get_value,
            table_exists=lambda doctype: doctype in self.tables,
            savepoint=self.savepoints.append,
            rollback=lambda save_point=None: self.rolled_back.append(save_point),
        )

    def get_single(self, doctype):
        return self.settings

    def get_doc(self, arg, name=None, for_update=False):
        if isinstance(arg, dict):
            return Doc(self, dict(arg))
        self.locked.append((arg, name, for_update))
        return self.rows[arg][name]

    def parse_json(self, text):
        return json.loads(text)

    def get_roles(self, user):
        return self.roles.get(user, [])

    def has_permission(self, doctype, ptype="read", doc=None, user=None):
        return (doctype, doc) not in self.unreadable

    def get_all(self, doctype, filters=None, pluck=None, limit_page_length=None, **kw):
        self.calls.append((doctype, filters, limit_page_length))
        if doctype == "Has Role":
            return [user for user, _enabled in self.has_role][:limit_page_length]
        if doctype == "User":
            return [
                u for u, enabled in self.has_role if enabled and u in filters["name"][1]
            ][:limit_page_length]
        found = self.matches.get(doctype, lambda f: [])(filters)
        return found[:limit_page_length] if limit_page_length else found

    def delete_doc(self, doctype, name, ignore_permissions=False):
        self.deleted.append((doctype, name))
        self.deleted_ignoring_permissions = ignore_permissions
        self.rows[doctype].pop(name)

    def _exists(self, doctype, name):
        return name in self.rows.get(doctype, {})

    def _count(self, doctype, filters):
        self.counts.append((doctype, filters))
        return self.auto_today

    def _get_value(self, doctype, name, fields):
        row = self.rows.get(doctype, {}).get(name)
        if row is None:
            return None
        if isinstance(fields, (list, tuple)):
            return tuple(getattr(row, f, None) for f in fields)
        return getattr(row, fields, None)


with mock.patch.dict(sys.modules, {"frappe": Frappe()}):
    from bi_finance import assistant_actions as actions


class PaymentRun(types.ModuleType):
    def __init__(self):
        super().__init__("bi_finance.payment_run")
        self.calls = []
        self.url = "http://site/app/payment-proposal/PP-0001"

    def create_payment_proposal(self, date=None, company=None, currency=None):
        self.calls.append((date, company, currency))
        return self.url


class VatDeclaration(types.ModuleType):
    def __init__(self, frappe):
        super().__init__("bi_finance.vat_declaration")
        self.frappe = frappe
        self.calls = []
        self.refuse = None

    def fill_payment_basis(self, company, period, dry_run=0):
        self.calls.append((company, period, int(dry_run)))
        if self.refuse:
            raise self.frappe.ValidationError(self.refuse)
        if int(dry_run):
            return {"fields": {"200": "1000.00"}}
        self.frappe.get_doc(
            {"doctype": "VAT Declaration", "name": "VAT-0001", "docstatus": 0}
        ).insert()
        return {"name": "VAT-0001", "fields": {"200": "1000.00"}}


def stub_ledger(name, level_class):
    """A ledger type that books nothing: its apply records the call and returns a document name or a text."""
    applied = []

    def apply(payload, dry_run):
        applied.append((payload, dry_run))
        return (
            f"would book {payload['amount']}"
            if dry_run
            else ("Journal Entry", "JV-0001")
        )

    spec = actions.Action(
        name,
        level_class,
        {
            "type": "object",
            "properties": {"amount": {"type": "string"}, "date": {"type": "string"}},
            "required": ["amount", "date"],
        },
        lambda payload, user, today: [],
        apply,
        lambda p: ("Journal Entry", ""),
        ledger_effect=True,
        amount=lambda p: float(p["amount"]),
        posting_date=lambda p: date.fromisoformat(p["date"]),
    )
    return spec, applied


class Base(unittest.TestCase):
    def setUp(self):
        self.frappe = Frappe()
        self.addCleanup(setattr, actions, "frappe", actions.frappe)
        actions.frappe = self.frappe
        self.frappe.roles = {USER: ["System Manager"], OTHER: ["AI Approver"]}
        self.one_click, self.one_click_applied = stub_ledger(
            "stub_book", levels.ONE_CLICK
        )
        self.always, self.always_applied = stub_ledger(
            "stub_submit", levels.ALWAYS_APPROVER
        )
        for spec in (self.one_click, self.always):
            self.addCleanup(actions.ACTIONS.pop, spec.name)
            actions.register(spec)
        self.payment_run = PaymentRun()
        self.vat = VatDeclaration(self.frappe)
        for name, module in (
            ("payment_run", self.payment_run),
            ("vat_declaration", self.vat),
        ):
            patcher = mock.patch.object(bi_finance, name, module, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def rows(self):
        return self.frappe.rows.get("AI Action", {})

    def row(self, result):
        return self.rows()[result["name"]]

    def propose(self, action_type, payload, score=1.0, user=USER):
        return actions.propose(user, "drafts", action_type, payload, score)

    def switch(self, user):
        self.frappe.session.user = user

    def book(self, amount="250.00", day="2027-03-01", action="stub_book", score=1.0):
        return self.propose(action, {"amount": amount, "date": day}, score)


class OneRowPerPath(Base):
    def test_every_path_writes_one_row(self):
        self.frappe.rows.setdefault("ToDo", {})["TD-1"] = Doc(
            self.frappe,
            {"doctype": "ToDo", "name": "TD-1", "status": "Open", "allocated_to": USER},
        )
        paths = [
            self.propose("add_comment", {}),  # arguments missing: refused
            self.propose("delete_doc", {}),  # never
            self.propose("no_such_type", {}),  # unregistered
            self.propose("close_todo", {"todo": "TD-1"}),  # level 0
            self.book(),  # level 1, left proposed
        ]
        self.assertEqual(len(self.rows()), len(paths))
        self.assertEqual(len({p["name"] for p in paths}), len(paths))

    def test_an_approval_updates_the_row_it_does_not_add_one(self):
        proposed = self.book()
        actions.approve(proposed["name"], USER)
        self.assertEqual(len(self.rows()), 1)


class Refusals(Base):
    def test_a_never_type_is_refused_and_logged(self):
        for name in ("delete_doc", "create_salary_slip", "write_bexio", "submit_draft"):
            result = self.propose(name, {"x": "y"})
            self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
            row = self.row(result)
            self.assertEqual((row.level, row.action_type), ("never", name))
            self.assertIn("never", row.error)

    def test_an_unregistered_type_is_refused_the_same_way(self):
        result = self.propose("transfer_money", {})
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.row(result).level, "never")
        self.assertIn("not an action", self.row(result).error)

    def test_the_kill_switch_refuses_every_proposal(self):
        self.frappe.settings.actions_enabled = 0
        result = self.propose(
            "add_comment",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-1",
                "content": "x",
            },
        )
        self.assertEqual(result["state"], "rejected")
        self.assertIn("switched off", self.row(result).error)

    def test_the_kill_switch_stops_an_approval_of_a_pending_action(self):
        proposed = self.book()
        self.frappe.settings.actions_enabled = 0
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (False, "proposed"))
        self.assertIn("switched off", self.row(proposed).error)
        self.assertEqual(self.one_click_applied, [])

    def test_a_proposal_in_another_users_session_is_refused(self):
        result = self.propose("add_comment", {}, user=OTHER)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(self.row(result).requested_by, OTHER)

    def test_an_argument_that_does_not_fit_the_schema_is_refused(self):
        result = self.propose(
            "add_comment",
            {"reference_doctype": "Sales Invoice", "content": "x", "extra": "y"},
        )
        self.assertEqual(result["state"], "rejected")
        error = self.row(result).error
        self.assertIn("reference_name is missing", error)
        self.assertIn("extra is not an argument", error)


class Ladder(Base):
    def test_a_one_click_ledger_type_within_limits_is_level_1_and_waits(self):
        result = self.book(day="2027-03-01")
        row = self.row(result)
        self.assertEqual(
            (result["state"], row.level, row.confidence), ("proposed", "1", 1.0)
        )
        self.assertEqual(self.one_click_applied, [])

    def test_an_amount_above_the_limit_moves_1_to_2(self):
        self.assertEqual(self.row(self.book(amount="1000.01")).level, "2")

    def test_a_low_score_moves_1_to_2(self):
        self.assertEqual(self.row(self.book(score=0.89)).level, "2")

    def test_an_always_approver_type_stays_2_at_099(self):
        self.assertEqual(
            self.row(self.book(action="stub_submit", score=0.99)).level, "2"
        )

    def test_a_posting_date_before_the_ledger_date_caps_the_confidence_and_lands_in_2(
        self,
    ):
        row = self.row(self.book(day="2026-12-31"))
        self.assertEqual((row.level, row.confidence), ("2", 0.5))
        check = next(c for c in row.checks if c["check"] == "ledger date")
        self.assertEqual((check["passed"], check["hard"], check["cap"]), (0, 1, 1))

    def test_the_ledger_date_is_the_setting(self):
        self.frappe.settings.ledger_actions_from = "2026-06-01"
        self.assertEqual(self.row(self.book(day="2026-12-31")).level, "1")

    def test_an_empty_ledger_date_is_the_default(self):
        self.frappe.settings.ledger_actions_from = None
        self.assertEqual(self.row(self.book(day="2026-12-31")).level, "2")
        self.assertEqual(self.row(self.book(day="2027-01-01")).level, "1")

    def test_a_check_that_raises_is_a_failed_structural_check(self):
        def broken(payload, user, today):
            raise KeyError("x")

        self.one_click.check = broken
        result = self.book()
        self.assertEqual(result["state"], "rejected")

    def test_the_row_carries_the_scores_the_checks_and_the_payload(self):
        row = self.row(self.book(score=0.95))
        self.assertEqual(
            (row.model_score, row.check_score, row.confidence), (0.95, 1.0, 0.95)
        )
        self.assertEqual(
            json.loads(row.payload), {"amount": "250.00", "date": "2027-03-01"}
        )
        self.assertEqual(
            (row.requested_by, row.task, row.amount), (USER, "drafts", 250.0)
        )
        self.assertTrue(
            all({"check", "passed", "hard", "detail"} <= set(c) for c in row.checks)
        )


class LevelOneApproval(Base):
    def test_the_requester_approves_and_it_applies(self):
        proposed = self.book()
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        row = self.row(proposed)
        self.assertEqual(
            (row.approver, row.result_doctype, row.result_name),
            (USER, "Journal Entry", "JV-0001"),
        )
        self.assertEqual(self.one_click_applied[0][1], False)

    def test_another_user_cannot_approve_level_1(self):
        proposed = self.book()
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER)
        self.assertEqual((result["ok"], result["state"]), (False, "proposed"))
        self.assertIn("user who asked", self.row(proposed).error)
        self.assertEqual(self.one_click_applied, [])

    def test_approving_in_another_users_session_is_refused(self):
        proposed = self.book()
        result = actions.approve(proposed["name"], OTHER)  # the session is still USER's
        self.assertFalse(result["ok"])
        self.assertEqual(self.row(proposed).state, "proposed")

    def test_the_row_is_read_for_update_so_two_clicks_cannot_both_apply(self):
        proposed = self.book()
        actions.approve(proposed["name"], USER)
        actions.reject(proposed["name"], USER)
        actions.undo(proposed["name"], USER)
        self.assertEqual(
            self.frappe.locked, [("AI Action", proposed["name"], True)] * 3
        )

    def test_an_approved_action_is_not_approved_twice(self):
        proposed = self.book()
        actions.approve(proposed["name"], USER)
        self.assertFalse(actions.approve(proposed["name"], USER)["ok"])
        self.assertEqual(len(self.one_click_applied), 1)

    def test_reject_by_the_requester_and_by_nobody_else(self):
        proposed = self.book()
        self.switch(OTHER)
        self.frappe.roles[OTHER] = []
        self.assertFalse(actions.reject(proposed["name"], OTHER)["ok"])
        self.switch(USER)
        result = actions.reject(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (True, "rejected"))
        self.assertFalse(actions.approve(proposed["name"], USER)["ok"])

    def test_an_approval_rechecks_and_a_structural_failure_refuses(self):
        proposed = self.book()
        self.one_click.check = lambda payload, user, today: [
            {
                "check": "target open",
                "passed": 0,
                "hard": 1,
                "cap": 0,
                "score": None,
                "detail": "closed since",
            }
        ]
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertIn("closed since", self.row(proposed).error)
        self.assertEqual(self.one_click_applied, [])


class LevelTwoApproval(Base):
    def setUp(self):
        super().setUp()
        self.proposed = self.book(
            amount="5000.00", day="2027-03-01"
        )  # above the limit: level 2

    def approve(self, user, amount="5000.00"):
        self.switch(user)
        return actions.approve(self.proposed["name"], user, amount)

    def test_it_is_level_2(self):
        self.assertEqual(self.row(self.proposed).level, "2")

    def test_the_requester_without_the_role_cannot_approve(self):
        self.assertFalse(self.approve(USER)["ok"])
        self.assertIn("AI Approver", self.row(self.proposed).error)

    def test_an_ai_approver_who_is_not_the_requester_approves_with_the_amount(self):
        result = self.approve(OTHER)
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        self.assertEqual(self.row(self.proposed).confirmed_amount, "5000.00")

    def test_a_wrong_confirmed_amount_is_refused(self):
        for typed in ("4900.00", None, "", "abc"):
            result = self.approve(OTHER, typed)
            self.assertEqual(
                (result["ok"], result["state"]), (False, "proposed"), typed
            )
        self.assertEqual(self.one_click_applied, [])

    def test_an_approval_after_a_refusal_clears_the_refusal(self):
        self.assertFalse(self.approve(OTHER, "4900.00")["ok"])
        result = self.approve(OTHER)
        self.assertEqual((result["ok"], result["message"]), (True, ""))
        self.assertFalse(self.row(self.proposed).error)

    def test_the_amount_compares_in_rappen(self):
        self.assertTrue(self.approve(OTHER, "5000")["ok"])

    def test_the_requester_cannot_approve_while_a_second_enabled_approver_exists(self):
        self.frappe.roles[USER] = ["AI Approver"]
        self.frappe.has_role = [(USER, 1), (OTHER, 1)]
        self.assertFalse(self.approve(USER)["ok"])
        self.assertIn("Another AI Approver", self.row(self.proposed).error)

    def test_the_requester_approves_when_no_other_approver_exists(self):
        self.frappe.roles[USER] = ["AI Approver"]
        self.frappe.has_role = [(USER, 1)]
        self.assertTrue(self.approve(USER)["ok"])

    def test_a_disabled_or_api_approver_does_not_count_as_a_second_one(self):
        self.frappe.roles[USER] = ["AI Approver"]
        self.frappe.has_role = [(USER, 1), (OTHER, 0), (API, 1)]
        self.assertTrue(self.approve(USER)["ok"])

    def test_the_api_user_cannot_approve_even_with_the_role(self):
        self.frappe.roles[API] = ["AI Approver", "System Manager"]
        result = self.approve(API)
        self.assertFalse(result["ok"])
        self.assertIn("API user", self.row(self.proposed).error)
        self.assertEqual(self.one_click_applied, [])

    def test_the_approver_check_reads_with_a_limit(self):
        self.frappe.roles[USER] = ["AI Approver"]
        self.frappe.has_role = [(USER, 1)]
        self.approve(USER)
        self.assertTrue(all(limit for _doctype, _filters, limit in self.frappe.calls))

    def test_an_ai_approver_may_reject(self):
        self.switch(OTHER)
        self.assertTrue(actions.reject(self.proposed["name"], OTHER)["ok"])


class LevelTwoTodos(Base):
    """A level 2 proposal puts a ToDo on each enabled AI Approver; deciding the action closes them (erp-ss1j)."""

    THIRD = "third@example.com"

    def setUp(self):
        super().setUp()
        self.frappe.matches["ToDo"] = lambda f: [
            name
            for name, todo in self.todos().items()
            if todo.reference_name == f["reference_name"] and todo.status == f["status"]
        ]

    def todos(self):
        return self.frappe.rows.get("ToDo", {})

    def owners(self):
        return sorted(todo.allocated_to for todo in self.todos().values())

    def level_two(self):
        return self.book(amount="5000.00", day="2027-03-01")

    def test_the_todos_go_to_the_approvers_but_not_the_requester(self):
        self.frappe.has_role = [(USER, 1), (OTHER, 1), (self.THIRD, 1)]
        self.level_two()
        self.assertEqual(self.owners(), [OTHER, self.THIRD])

    def test_the_todo_points_at_the_action_and_names_no_amount(self):
        self.frappe.has_role = [(OTHER, 1)]
        proposed = self.level_two()
        todo = next(iter(self.todos().values()))
        self.assertEqual(
            (todo.reference_type, todo.reference_name, todo.status),
            ("AI Action", proposed["name"], "Open"),
        )
        self.assertIn("stub_book", todo.description)
        self.assertNotIn("5000", todo.description)

    def test_the_requester_gets_the_todo_when_the_only_approver(self):
        self.frappe.has_role = [(USER, 1)]
        self.level_two()
        self.assertEqual(self.owners(), [USER])

    def test_a_disabled_approver_and_the_api_user_get_none(self):
        self.frappe.has_role = [(OTHER, 0), (API, 1), (self.THIRD, 1)]
        self.level_two()
        self.assertEqual(self.owners(), [self.THIRD])

    def test_no_approver_means_no_todo(self):
        self.level_two()
        self.assertEqual(self.owners(), [])

    def test_levels_0_and_1_make_no_todo(self):
        self.frappe.has_role = [(OTHER, 1)]
        self.book()  # 250.00: level 1
        self.assertEqual(self.owners(), [])

    def test_approving_closes_the_todos(self):
        self.frappe.has_role = [(OTHER, 1), (self.THIRD, 1)]
        proposed = self.level_two()
        self.frappe.roles[OTHER] = ["AI Approver"]
        self.switch(OTHER)
        self.assertTrue(actions.approve(proposed["name"], OTHER, "5000.00")["ok"])
        self.assertEqual({t.status for t in self.todos().values()}, {"Closed"})

    def test_a_wrong_amount_keeps_the_todos_open(self):
        self.frappe.has_role = [(OTHER, 1)]
        proposed = self.level_two()
        self.frappe.roles[OTHER] = ["AI Approver"]
        self.switch(OTHER)
        self.assertFalse(actions.approve(proposed["name"], OTHER, "4900.00")["ok"])
        self.assertEqual({t.status for t in self.todos().values()}, {"Open"})

    def test_rejecting_closes_the_todos(self):
        self.frappe.has_role = [(OTHER, 1)]
        proposed = self.level_two()
        self.frappe.roles[OTHER] = ["AI Approver"]
        self.switch(OTHER)
        self.assertTrue(actions.reject(proposed["name"], OTHER)["ok"])
        self.assertEqual({t.status for t in self.todos().values()}, {"Closed"})


class FormButtons(Base):
    """can_decide is the rule the AI Action form's onload hands the Approve / Reject buttons (erp-ss1j)."""

    def setUp(self):
        super().setUp()
        self.frappe.roles[USER] = ["AI Approver"]
        self.frappe.roles[API] = ["AI Approver"]
        self.doc = self.row(self.book(amount="5000.00", day="2027-03-01"))

    def test_an_approver_sees_them_on_a_proposed_level_2_action(self):
        self.assertTrue(actions.can_decide(self.doc, OTHER))

    def test_the_requester_sees_them_only_as_the_sole_approver(self):
        self.frappe.has_role = [(USER, 1)]
        self.assertTrue(actions.can_decide(self.doc, USER))
        self.frappe.has_role = [(USER, 1), (OTHER, 1)]
        self.assertFalse(actions.can_decide(self.doc, USER))

    def test_a_user_without_the_role_and_the_api_user_do_not(self):
        self.assertFalse(actions.can_decide(self.doc, "nobody@example.com"))
        self.assertFalse(actions.can_decide(self.doc, API))

    def test_nobody_sees_them_once_decided(self):
        self.switch(OTHER)
        actions.approve(self.doc.name, OTHER, "5000.00")
        self.assertFalse(actions.can_decide(self.doc, OTHER))

    def test_nobody_sees_them_on_a_level_1_action(self):
        level_one = self.row(self.book())
        self.assertFalse(actions.can_decide(level_one, OTHER))


class BeforeTheLedgerDate(Base):
    def test_a_ledger_action_ends_approved_dry_run_and_writes_nothing(self):
        proposed = self.book(amount="5000.00", day="2026-12-31")
        self.assertEqual(self.row(proposed).level, "2")
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, "5000.00")
        self.assertEqual((result["ok"], result["state"]), (True, "approved-dry-run"))
        row = self.row(proposed)
        self.assertEqual(row.dry_run_result, "would book 5000.00")
        self.assertFalse(getattr(row, "result_name", None))
        self.assertEqual(
            self.one_click_applied,
            [({"amount": "5000.00", "date": "2026-12-31"}, True)],
        )
        self.assertNotIn("Journal Entry", self.frappe.rows)

    def test_the_same_action_on_the_ledger_date_applies_for_real(self):
        proposed = self.book(amount="5000.00", day="2027-01-01")
        self.switch(OTHER)
        self.assertEqual(
            actions.approve(proposed["name"], OTHER, "5000.00")["state"], "approved"
        )
        self.assertEqual(self.one_click_applied[0][1], False)

    def test_a_level_1_ledger_action_before_the_date_is_a_dry_run_too(self):
        self.frappe.settings.ledger_actions_from = "2026-06-01"
        proposed = self.book(day="2026-07-01")  # level 1 under the earlier ledger date
        self.frappe.settings.ledger_actions_from = (
            "2027-01-01"  # moved forward before the click
        )
        self.assertEqual(
            actions.approve(proposed["name"], USER)["state"], "approved-dry-run"
        )

    def test_a_ledger_type_cannot_be_auto(self):
        with self.assertRaises(ValueError):
            actions.Action(
                "x",
                levels.AUTO,
                {},
                None,
                None,
                None,
                ledger_effect=True,
                posting_date=lambda p: None,
            )


class Failures(Base):
    def test_a_failing_apply_ends_failed_and_rolls_back(self):
        def boom(payload, dry_run):
            raise self.frappe.ValidationError("the bank said no")

        self.one_click.apply = boom
        proposed = self.book()
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (False, "failed"))
        self.assertIn("the bank said no", self.row(proposed).error)
        self.assertEqual(self.frappe.rolled_back, [actions.SAVEPOINT])

    def test_apply_runs_in_the_session_of_the_user(self):
        seen = []
        self.one_click.apply = lambda payload, dry_run: seen.append(
            self.frappe.session.user
        ) or ("Journal Entry", "JV-9")
        proposed = self.book()
        actions.approve(proposed["name"], USER)
        self.assertEqual(seen, [USER])
        self.assertFalse(hasattr(self.frappe, "set_user"))


class AutoLevel(Base):
    def comment(self, content="Called the customer."):
        self.frappe.rows.setdefault("Sales Invoice", {})["SI-1"] = Doc(
            self.frappe, {"doctype": "Sales Invoice", "name": "SI-1"}
        )
        return self.propose(
            "add_comment",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-1",
                "content": content,
            },
        )

    def test_add_comment_applies_at_once_and_is_logged(self):
        result = self.comment()
        self.assertEqual((result["ok"], result["state"]), (True, "auto-done"))
        row = self.row(result)
        self.assertEqual(
            (row.level, row.result_doctype, row.target_name), ("0", "Comment", "SI-1")
        )
        comment = self.frappe.rows["Comment"][row.result_name]
        self.assertEqual(
            (comment.reference_name, comment.content), ("SI-1", "Called the customer.")
        )

    def test_a_low_model_score_waits_for_one_click(self):
        self.frappe.rows.setdefault("Sales Invoice", {})["SI-1"] = Doc(
            self.frappe, {"doctype": "Sales Invoice", "name": "SI-1"}
        )
        result = self.propose(
            "add_comment",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-1",
                "content": "x",
            },
            score=0.79,
        )
        self.assertEqual((result["state"], self.row(result).level), ("proposed", "1"))
        self.assertNotIn("Comment", self.frappe.rows)
        self.assertTrue(actions.approve(result["name"], USER)["ok"])
        self.assertIn("Comment", self.frappe.rows)

    def test_auto_off_moves_level_0_to_1(self):
        self.frappe.settings.auto_level_enabled = 0
        self.assertEqual(self.comment()["state"], "proposed")

    def test_the_daily_cap_moves_level_0_to_1(self):
        self.frappe.auto_today = 50
        self.assertEqual(self.comment()["state"], "proposed")
        self.frappe.auto_today = 49
        self.assertEqual(self.comment()["state"], "auto-done")

    def test_the_daily_count_is_one_count_of_the_users_level_0_rows_today(self):
        self.comment()
        doctype, filters = self.frappe.counts[0]
        self.assertEqual(doctype, "AI Action")
        self.assertEqual(filters["requested_by"], USER)
        self.assertEqual(filters["level"], "0")
        self.assertEqual(filters["creation"], [">=", TODAY])

    def test_a_comment_on_a_document_the_user_cannot_read_is_refused(self):
        self.frappe.unreadable.add(("Sales Invoice", "SI-1"))
        result = self.comment()
        self.assertEqual(result["state"], "rejected")
        self.assertNotIn("Comment", self.frappe.rows)

    def test_a_comment_on_a_missing_document_is_refused(self):
        result = self.propose(
            "add_comment",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-404",
                "content": "x",
            },
        )
        self.assertEqual(result["state"], "rejected")

    def test_an_oversize_comment_is_refused(self):
        self.assertEqual(self.comment("x" * 501)["state"], "rejected")

    def test_a_failing_level_0_apply_ends_failed(self):
        self.frappe.rows.setdefault("Sales Invoice", {})["SI-1"] = Doc(
            self.frappe, {"doctype": "Sales Invoice", "name": "SI-1"}
        )
        self.frappe.rows["Sales Invoice"]["SI-1"].add_comment = mock.Mock(
            side_effect=ValueError("locked")
        )
        result = self.propose(
            "add_comment",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-1",
                "content": "x",
            },
        )
        self.assertEqual((result["ok"], result["state"]), (False, "failed"))
        self.assertIn("locked", self.row(result).error)

    def test_undo_deletes_the_comment(self):
        result = self.comment()
        name = self.row(result).result_name
        undone = actions.undo(result["name"], USER)
        self.assertEqual((undone["ok"], undone["state"]), (True, "undone"))
        self.assertEqual(self.frappe.deleted, [("Comment", name)])
        # Comment is deletable by System Manager only, yet add_comment ignored permissions: so does its Undo
        self.assertTrue(self.frappe.deleted_ignoring_permissions)

    def test_undo_is_refused_when_the_comment_changed(self):
        result = self.comment()
        self.frappe.rows["Comment"][self.row(result).result_name].comment_type = "Info"
        undone = actions.undo(result["name"], USER)
        self.assertEqual((undone["ok"], undone["state"]), (False, "auto-done"))
        self.assertEqual(self.frappe.deleted, [])

    def test_undo_by_a_stranger_is_refused(self):
        result = self.comment()
        self.switch(OTHER)
        self.frappe.roles[OTHER] = []
        self.assertFalse(actions.undo(result["name"], OTHER)["ok"])
        self.assertEqual(self.row(result).state, "auto-done")

    def test_undo_is_allowed_with_the_kill_switch_off(self):
        result = self.comment()
        self.frappe.settings.actions_enabled = 0
        self.assertTrue(actions.undo(result["name"], USER)["ok"])

    def test_a_proposed_action_has_no_undo(self):
        proposed = self.book()
        self.assertFalse(actions.undo(proposed["name"], USER)["ok"])


class Todos(Base):
    def todo(self, **values):
        row = Doc(
            self.frappe,
            {
                "doctype": "ToDo",
                "name": "TD-7",
                "status": "Open",
                "allocated_to": USER,
                **values,
            },
        )
        self.frappe.rows.setdefault("ToDo", {})["TD-7"] = row
        return row

    def test_create_todo_is_for_the_user_and_undo_cancels_it(self):
        result = self.propose(
            "create_todo",
            {
                "description": "Call about the reminder",
                "date": "2027-02-20",
                "priority": "High",
            },
        )
        self.assertEqual(result["state"], "auto-done")
        row = self.row(result)
        todo = self.frappe.rows["ToDo"][row.result_name]
        self.assertEqual(
            (todo.allocated_to, todo.status, todo.priority), (USER, "Open", "High")
        )
        self.assertTrue(actions.undo(result["name"], USER)["ok"])
        self.assertEqual(todo.status, "Cancelled")

    def test_create_todo_refuses_a_bad_day_priority_and_task_for_somebody_else(self):
        self.assertEqual(
            self.propose("create_todo", {"description": "x", "date": "tomorrow"})[
                "state"
            ],
            "rejected",
        )
        self.assertEqual(
            self.propose("create_todo", {"description": "x", "priority": "Urgent"})[
                "state"
            ],
            "rejected",
        )
        self.assertEqual(
            self.propose("create_todo", {"description": "x", "allocated_to": OTHER})[
                "state"
            ],
            "rejected",
        )

    def test_create_todo_undo_is_refused_once_the_todo_moved_on(self):
        result = self.propose("create_todo", {"description": "x"})
        self.frappe.rows["ToDo"][self.row(result).result_name].status = "Closed"
        self.assertFalse(actions.undo(result["name"], USER)["ok"])

    def test_close_todo_closes_an_own_open_todo_and_undo_reopens_it(self):
        todo = self.todo()
        result = self.propose("close_todo", {"todo": "TD-7"})
        self.assertEqual((result["state"], todo.status), ("auto-done", "Closed"))
        self.assertTrue(actions.undo(result["name"], USER)["ok"])
        self.assertEqual(todo.status, "Open")

    def test_close_todo_refuses_somebody_elses_todo(self):
        todo = self.todo(allocated_to=OTHER)
        self.assertEqual(
            self.propose("close_todo", {"todo": "TD-7"})["state"], "rejected"
        )
        self.assertEqual(todo.status, "Open")

    def test_close_todo_refuses_a_todo_that_is_not_open(self):
        self.todo(status="Closed")
        self.assertEqual(
            self.propose("close_todo", {"todo": "TD-7"})["state"], "rejected"
        )


class PaymentProposals(Base):
    def setUp(self):
        super().setUp()
        self.frappe.matches["Purchase Invoice"] = lambda f: ["PINV-1"]

    def test_it_creates_the_draft_through_payment_run_and_undo_deletes_it_while_a_draft(
        self,
    ):
        self.frappe.get_doc(
            {"doctype": "Payment Proposal", "name": "PP-0001", "docstatus": 0}
        ).insert()
        result = self.propose("create_payment_proposal", {})
        self.assertEqual(
            (result["state"], self.row(result).result_name), ("auto-done", "PP-0001")
        )
        self.assertEqual(self.payment_run.calls, [("2027-02-24", "Invented Co", None)])
        self.assertTrue(actions.undo(result["name"], USER)["ok"])
        self.assertEqual(self.frappe.deleted, [("Payment Proposal", "PP-0001")])

    def test_undo_is_refused_once_the_proposal_is_submitted(self):
        self.frappe.get_doc(
            {"doctype": "Payment Proposal", "name": "PP-0001", "docstatus": 1}
        ).insert()
        result = self.propose("create_payment_proposal", {})
        self.assertFalse(actions.undo(result["name"], USER)["ok"])
        self.assertEqual(self.frappe.deleted, [])

    def test_nothing_due_is_refused(self):
        self.frappe.matches.clear()
        self.assertEqual(
            self.propose("create_payment_proposal", {})["state"], "rejected"
        )
        self.assertEqual(self.payment_run.calls, [])

    def test_expense_claims_count_when_hrms_is_there(self):
        self.frappe.matches.clear()
        self.frappe.tables.add("Expense Claim")
        self.frappe.matches["Expense Claim"] = lambda f: ["EC-1"]
        self.assertEqual(
            self.propose("create_payment_proposal", {})["state"], "auto-done"
        )

    def test_a_draft_proposal_already_there_caps_it_to_level_1(self):
        self.frappe.matches["Payment Proposal"] = lambda f: ["PP-0009"]
        result = self.propose("create_payment_proposal", {})
        row = self.row(result)
        self.assertEqual(
            (result["state"], row.level, row.confidence), ("proposed", "1", 0.5)
        )
        self.assertEqual(self.payment_run.calls, [])

    def test_every_read_has_a_limit(self):
        self.propose("create_payment_proposal", {})
        self.assertTrue(all(limit == 1 for _d, _f, limit in self.frappe.calls))

    def test_a_proposal_run_that_makes_none_fails(self):
        self.payment_run.url = None
        self.assertEqual(self.propose("create_payment_proposal", {})["state"], "failed")


class VatDrafts(Base):
    PAYLOAD = {"company": "Invented Co", "period": "2026Q4"}

    def test_it_creates_the_draft_after_a_dry_run_and_undo_deletes_it(self):
        result = self.propose("create_vat_draft", self.PAYLOAD)
        self.assertEqual(
            (result["state"], self.row(result).result_name), ("auto-done", "VAT-0001")
        )
        self.assertEqual(
            self.vat.calls, [("Invented Co", "2026Q4", 1), ("Invented Co", "2026Q4", 0)]
        )
        self.assertTrue(actions.undo(result["name"], USER)["ok"])
        self.assertEqual(self.frappe.deleted, [("VAT Declaration", "VAT-0001")])

    def test_undo_is_refused_once_the_declaration_is_submitted(self):
        result = self.propose("create_vat_draft", self.PAYLOAD)
        self.frappe.rows["VAT Declaration"]["VAT-0001"].docstatus = 1
        self.assertFalse(actions.undo(result["name"], USER)["ok"])

    def test_a_quarter_that_has_not_ended_is_refused(self):
        self.assertEqual(
            self.propose(
                "create_vat_draft", {"company": "Invented Co", "period": "2027Q1"}
            )["state"],
            "rejected",
        )
        self.assertEqual([c for c in self.vat.calls if c[2] == 0], [])

    def test_a_bad_period_is_refused(self):
        self.assertEqual(
            self.propose(
                "create_vat_draft", {"company": "Invented Co", "period": "2026X"}
            )["state"],
            "rejected",
        )

    def test_a_submitted_declaration_refuses(self):
        self.frappe.matches["VAT Declaration"] = lambda f: (
            ["VAT-1"] if f["docstatus"] == 1 else []
        )
        self.assertEqual(
            self.propose("create_vat_draft", self.PAYLOAD)["state"], "rejected"
        )

    def test_a_draft_already_there_caps_it_to_level_1(self):
        self.frappe.matches["VAT Declaration"] = lambda f: (
            ["VAT-1"] if f["docstatus"] == 0 else []
        )
        result = self.propose("create_vat_draft", self.PAYLOAD)
        self.assertEqual((result["state"], self.row(result).level), ("proposed", "1"))

    def test_a_dry_run_that_the_fill_refuses_refuses_the_action(self):
        self.vat.refuse = "Tax ID missing"
        result = self.propose("create_vat_draft", self.PAYLOAD)
        self.assertEqual(result["state"], "rejected")
        self.assertIn("Tax ID missing", self.row(result).error)
        self.assertEqual([c for c in self.vat.calls if c[2] == 0], [])


class Registry(unittest.TestCase):
    def test_the_five_level_0_types_are_registered_with_schemas_and_undo(self):
        for name in (
            "add_comment",
            "create_todo",
            "close_todo",
            "create_payment_proposal",
            "create_vat_draft",
        ):
            spec = actions.ACTIONS[name]
            self.assertEqual(
                (spec.level_class, spec.ledger_effect), (levels.AUTO, False), name
            )
            self.assertEqual(spec.schema["type"], "object", name)
            self.assertFalse(spec.schema["additionalProperties"], name)
            self.assertIsNotNone(spec.undo, name)

    def test_no_never_type_is_registered(self):
        self.assertEqual(set(actions.NEVER_ACTIONS) & set(actions.ACTIONS), set())

    def test_payroll_and_free_form_drafts_are_never(self):
        for name in (
            "create_payroll_entry",
            "submit_salary_slip",
            "create_draft",
            "submit_draft",
            "cancel_doc",
        ):
            self.assertIn(name, actions.NEVER_ACTIONS)


class Role(unittest.TestCase):
    def test_the_role_patch_is_idempotent(self):
        frappe = Frappe()
        created = []
        frappe.db.exists = lambda doctype, name: name in {
            r["role_name"] for r in created
        }
        frappe.get_doc = lambda values: types.SimpleNamespace(
            insert=lambda ignore_permissions=False: created.append(values)
        )
        with mock.patch.dict(sys.modules, {"frappe": frappe}):
            from bi_finance.patches import ai_approver_role

        with contextlib.redirect_stdout(io.StringIO()) as out:
            ai_approver_role.execute()
            ai_approver_role.execute()
        self.assertEqual(out.getvalue().count("created"), 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["role_name"], "AI Approver")


class AutoLevelDefault(unittest.TestCase):
    def run_patch(self, stored):
        frappe = Frappe()
        frappe.db.sql = lambda query, values: [(field,) for field in stored]
        frappe.db.set_single_value = lambda doctype, field, value: stored.__setitem__(
            field, value
        )
        with mock.patch.dict(sys.modules, {"frappe": frappe}):
            from bi_finance.patches import ai_auto_level_default
        with contextlib.redirect_stdout(io.StringIO()):
            ai_auto_level_default.execute()
            ai_auto_level_default.execute()
        return stored

    def test_a_single_saved_before_the_field_gets_level_0_on(self):
        stored = self.run_patch({"enabled": 1, "model": "m"})
        self.assertEqual(stored["auto_level_enabled"], 1)

    def test_a_single_saved_before_the_section_gets_the_ladder_defaults(self):
        stored = self.run_patch({"enabled": 1, "model": "m"})
        self.assertEqual(stored["auto_threshold"], 0.80)
        self.assertEqual(stored["one_click_threshold"], 0.90)
        self.assertEqual(stored["one_click_amount_limit"], 1000.0)
        self.assertEqual(stored["auto_actions_per_day"], 50)
        self.assertEqual(stored["ledger_actions_from"], date(2027, 1, 1))

    def test_a_stored_value_is_kept(self):
        stored = self.run_patch(
            {"enabled": 1, "auto_level_enabled": 0, "auto_actions_per_day": 3}
        )
        self.assertEqual(stored["auto_level_enabled"], 0)
        self.assertEqual(stored["auto_actions_per_day"], 3)

    def test_a_single_never_saved_keeps_its_defaults(self):
        self.assertEqual(self.run_patch({}), {})


class AuditLogPermissions(unittest.TestCase):
    def test_an_ai_approver_reads_the_log_but_cannot_write_it(self):
        path = os.path.join(
            os.path.dirname(__file__),
            "bi_finance",
            "doctype",
            "ai_action",
            "ai_action.json",
        )
        with open(path, encoding="utf-8") as f:
            perms = [
                p for p in json.load(f)["permissions"] if p["role"] == "AI Approver"
            ]
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0]["read"], 1)
        self.assertEqual(perms[0]["write"], 0)


if __name__ == "__main__":
    unittest.main()
