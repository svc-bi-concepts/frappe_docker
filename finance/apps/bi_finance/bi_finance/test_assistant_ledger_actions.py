"""The desk assistant's four ledger actions (erp-9f7y): their hard checks, their check scores and the dry run.

Invented parties, documents and round amounts only. frappe is a stub (the one of test_assistant_actions, with the
recorded full calls added), bank_match_job, payment_run and vat_declaration are stubs that record what the actions call,
and bank_match, mwst_quarterly and mwst_payment are the real pure modules. Runs on the host, in the light gate:

    python3 -m unittest bi_finance.test_assistant_ledger_actions

Pinned, per action: each hard check failing (refuse, or cap at 0.5 for a duplicate and the ledger date); each row of the
check score table; a dry run before the ledger date that writes and submits nothing and returns text; and after it the
call path of the real action (the job's _book, the document's submit).
"""

import datetime
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

import bi_finance
from bi_finance import assistant_levels as levels
from bi_finance import mwst_quarterly
from bi_finance.test_assistant_actions import (
    API,
    OTHER,
    TODAY,
    USER,
    Doc,
    Frappe,
    PaymentRun,
    VatDeclaration,
    actions,
)

# assistant_actions imports the ledger module at its end, under test_assistant_actions' stub frappe, so the module is
# there once the import above has run; an import statement here would come first (isort) and find no frappe
ledger = bi_finance.assistant_ledger_actions

# SIX's documentation IBAN of Switzerland, and the same with a wrong check digit
IBAN = "CH93 0076 2011 6238 5295 7"
BAD_IBAN = "CH93 0076 2011 6238 5295 8"


def iban_is_valid(iban):
    """payment_run.iban_is_valid (the module imports erpnextswiss, which only the image has): the ISO 7064 mod 97."""
    iban = iban.replace(" ", "").upper()
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


class Row(dict):
    """A child row or a get_all row: keys read as attributes, as frappe's _dict does."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self.get(name)


class Child(Row):
    """A child row of a stored document: get() and attributes but no [], as frappe's Document has no __getitem__."""

    def __getitem__(self, key):
        raise TypeError(f"'{type(self).__name__}' object is not subscriptable")


class Record(Doc):
    """A stored document of the ledger types: get() as frappe's Document has it, and a submit that is recorded."""

    def get(self, name, default=None):
        value = self.__dict__.get(name)
        return default if value is None else value

    def submit(self):
        self._frappe.submitted.append((self.doctype, self.name))
        self.docstatus = 1


class LedgerFrappe(Frappe):
    def __init__(self):
        super().__init__()
        self.submitted = []
        self.reads = (
            []
        )  # (doctype, filters, limit, the other arguments) of every get_all

    def get_all(self, doctype, filters=None, pluck=None, limit_page_length=None, **kw):
        self.reads.append((doctype, filters, limit_page_length, kw))
        return super().get_all(doctype, filters, pluck, limit_page_length, **kw)


class BankMatchJob(types.ModuleType):
    """bank_match_job without erpnext: the reads return what the test lays out, the booking is recorded."""

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
        self.docs = []  # the open invoices and bills, in bank_match's shape
        self.payments = []  # the unallocated Payment Entries
        self.opened = []  # (kind, company, currency, user, name) of every read
        self.booked = []
        self.preview = []

    def _open_documents(self, company, currency, user=None, name=None):
        self.opened.append(("documents", company, currency, user, name))
        return [doc for doc in self.docs if doc["name"] == name]

    def _open_payments(self, company, currency, gl_account, user=None, name=None):
        self.opened.append(("payments", company, currency, user, name))
        return [doc for doc in self.payments if doc["name"] == name]

    def _book(self, line, decision, gl_account):
        self.booked.append((dict(line), decision, gl_account))
        return "PE-0001"

    def revaluation_preview(self, company, posting_date):
        return self.preview


def open_invoice(**over):
    doc = {
        "doctype": "Sales Invoice",
        "name": "SI-1",
        "party_name": "Invented Customer AG",
        "direction": "in",
        "amount": 500.0,
        "currency": "CHF",
        "date": "2027-02-05",
        "reference": None,
    }
    return {**doc, **over}


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.frappe = LedgerFrappe()
        for module in (actions, ledger):
            self.addCleanup(setattr, module, "frappe", module.frappe)
            module.frappe = self.frappe
        self.frappe.roles = {USER: ["System Manager"], OTHER: ["AI Approver"]}
        self.frappe.has_role = [(OTHER, True)]
        self.job = BankMatchJob()
        self.payment_run = PaymentRun()
        self.payment_run.iban_is_valid = iban_is_valid
        self.vat = VatDeclaration(self.frappe)
        self.vat._payment_totals = lambda company, start, end: {"period": (start, end)}
        for name, module in (
            ("bank_match_job", self.job),
            ("payment_run", self.payment_run),
            ("vat_declaration", self.vat),
        ):
            patcher = mock.patch.object(bi_finance, name, module, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- laying out the stub site

    def add(self, doctype, name, **values):
        values.setdefault("docstatus", 0)
        record = Record(self.frappe, {"doctype": doctype, "name": name, **values})
        self.frappe.rows.setdefault(doctype, {})[name] = record
        return record

    def rows(self):
        return self.frappe.rows.get("AI Action", {})

    def row(self, result):
        return self.rows()[result["name"]]

    def check(self, result, name):
        found = [c for c in self.row(result).checks if c["check"] == name]
        self.assertEqual(
            len(found), 1, f"{name}: {[c['check'] for c in self.row(result).checks]}"
        )
        return found[0]

    def propose(self, action_type, payload, score=1.0, user=USER):
        return actions.propose(user, "drafts", action_type, payload, score)

    def switch(self, user):
        self.frappe.session.user = user

    def applied(self):
        """Everything that wrote or submitted, apart from the AI Action rows themselves."""
        writes = [w for w in self.frappe.writes if w[0] != "AI Action"]
        return writes, self.frappe.submitted, self.job.booked


# ================================================================ match_bank_transaction


class MatchBase(LedgerBase):
    def setUp(self):
        super().setUp()
        self.add("Bank Account", "BA-1", account="1020 Bank", company="Invented Co")
        self.add("Account", "1020 Bank", account_currency="CHF")
        self.line = self.bank_line()
        self.invoice = self.add(
            "Sales Invoice",
            "SI-1",
            customer_name="Invented Customer AG",
            debit_to="1100 Debtors",
        )
        self.job.docs = [open_invoice()]

    def bank_line(self, name="BT-1", **over):
        values = {
            "date": date(2027, 2, 8),
            "deposit": 500.0,
            "withdrawal": 0.0,
            "description": "unrelated text",
            "reference_number": "",
            "transaction_id": "",
            "end_to_end_id": "",
            "currency": "CHF",
            "company": "Invented Co",
            "bank_account": "BA-1",
            "status": "Unreconciled",
            "docstatus": 1,
            **over,
        }
        return self.add("Bank Transaction", name, **values)

    def match(self, doctype="Sales Invoice", name="SI-1", line="BT-1", **kw):
        return self.propose(
            "match_bank_transaction",
            {"bank_transaction": line, "doctype": doctype, "name": name},
            **kw,
        )


class MatchHardChecks(MatchBase):
    def refused(self, result, check):
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.row(result).level, "never")
        self.assertEqual(self.check(result, check)["passed"], 0)

    def test_a_reconciled_line_is_refused(self):
        self.line.status = "Reconciled"
        self.refused(self.match(), "line unreconciled")

    def test_a_cancelled_line_is_refused(self):
        self.line.docstatus = 2
        self.refused(self.match(), "line unreconciled")

    def test_a_line_without_a_bank_account_is_refused(self):
        self.line.bank_account = None
        self.refused(self.match(), "line unreconciled")

    def test_a_target_that_is_not_open_is_refused(self):
        self.job.docs = []
        self.refused(self.match(), "target open")

    def test_another_currency_is_refused(self):
        self.job.docs = [open_invoice(currency="EUR")]
        self.refused(self.match(), "same currency")

    def test_the_wrong_direction_is_refused(self):
        # a withdrawal does not pay a Sales Invoice
        self.line.deposit, self.line.withdrawal = 0.0, 500.0
        self.refused(self.match(), "same direction")

    def test_a_partial_amount_is_refused(self):
        self.job.docs = [open_invoice(amount=800.0)]
        self.refused(self.match(), "amount equals the outstanding")

    def test_an_amount_a_rappen_off_is_refused(self):
        self.job.docs = [open_invoice(amount=500.01)]
        self.refused(self.match(), "amount equals the outstanding")

    def test_an_unreadable_line_or_target_is_refused_and_nothing_else_is_read(self):
        self.frappe.unreadable.add(("Bank Transaction", "BT-1"))
        result = self.match()
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(self.check(result, "line readable")["passed"], 0)
        self.assertEqual(self.job.opened, [])

    def test_an_unreadable_target_is_refused(self):
        self.frappe.unreadable.add(("Sales Invoice", "SI-1"))
        self.refused(self.match(), "target readable")

    def test_a_missing_argument_is_refused(self):
        result = self.propose("match_bank_transaction", {"bank_transaction": "BT-1"})
        self.assertEqual(result["state"], "rejected")
        self.assertIn("doctype is missing", self.row(result).error)

    def test_a_doctype_outside_the_three_is_refused(self):
        result = self.match(doctype="Journal Entry")
        self.assertEqual(result["state"], "rejected")
        self.assertIn("must be one of", self.row(result).error)


class MatchShared(MatchBase):
    def other_line(self, **over):
        return Row(
            {
                "name": "BT-2",
                "date": date(2027, 2, 8),
                "deposit": 500.0,
                "withdrawal": 0.0,
                "description": "Zahlung Invented Customer",
                "reference_number": "",
                "transaction_id": "",
                "end_to_end_id": "",
                "company": "Invented Co",
                **over,
            }
        )

    def test_another_line_that_names_the_target_caps_the_confidence_at_half(self):
        self.frappe.matches["Bank Transaction"] = lambda filters: [self.other_line()]
        result = self.match()
        # a duplicate is no structural failure: the proposal waits for an approver
        self.assertEqual((result["ok"], result["state"]), (True, "proposed"))
        row = self.row(result)
        self.assertEqual((row.level, row.confidence), ("2", 0.5))
        shared = self.check(result, "no other line names it")
        self.assertEqual((shared["passed"], shared["hard"], shared["cap"]), (0, 1, 1))

    def test_two_other_lines_that_name_it_count_too(self):
        # decide() calls the document shared for both of them, which is as good as named by each
        self.frappe.matches["Bank Transaction"] = lambda filters: [
            self.other_line(),
            self.other_line(name="BT-3"),
        ]
        self.assertEqual(self.row(self.match()).level, "2")

    def test_a_line_that_names_the_target_by_reference_counts_even_when_ours_is_amount_only(
        self,
    ):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.frappe.matches["Bank Transaction"] = lambda filters: [
            self.other_line(description="x", reference_number="RF18 5390")
        ]
        result = self.match()
        self.assertEqual(self.check(result, "no other line names it")["passed"], 0)
        self.assertEqual(self.row(result).confidence, 0.5)

    def test_a_line_that_names_nothing_is_no_claim(self):
        self.frappe.matches["Bank Transaction"] = lambda filters: [
            self.other_line(description="Other Supplier", date=date(2027, 2, 8))
        ]
        self.assertEqual(
            self.check(self.match(), "no other line names it")["passed"], 1
        )

    def test_a_party_named_outside_the_anchor_is_no_claim(self):
        self.frappe.matches["Bank Transaction"] = lambda filters: [
            self.other_line(date=date(2027, 3, 20))
        ]
        self.assertEqual(
            self.check(self.match(), "no other line names it")["passed"], 1
        )

    def test_the_other_lines_are_read_bounded_by_account_and_amount(self):
        self.match()
        doctype, filters, limit, kw = [
            r for r in self.frappe.reads if r[0] == "Bank Transaction"
        ][0]
        self.assertEqual(limit, ledger.SHARED_LINES_LIMIT)
        self.assertEqual(filters["bank_account"], "BA-1")
        self.assertEqual(filters["status"], "Unreconciled")
        self.assertEqual(filters["name"], ["!=", "BT-1"])
        self.assertEqual(
            kw["or_filters"], [["deposit", "=", 500.0], ["withdrawal", "=", 500.0]]
        )
        self.assertEqual(kw["fields"], BankMatchJob.LINE_FIELDS)

    def test_the_target_is_read_alone_for_the_user(self):
        self.match()
        self.assertEqual(
            self.job.opened, [("documents", "Invented Co", None, USER, "SI-1")]
        )

    def test_too_many_lines_of_this_amount_cannot_be_told_and_cap(self):
        self.frappe.matches["Bank Transaction"] = lambda filters: [
            self.other_line(name=f"BT-{n}", description="x")
            for n in range(ledger.SHARED_LINES_LIMIT)
        ]
        result = self.match()
        self.assertEqual(self.check(result, "no other line names it")["passed"], 0)
        self.assertEqual(self.row(result).level, "2")


class MatchScores(MatchBase):
    """The check score table of erp-00z0: the strongest passed row is the score; a model score of 1 leaves it as it is."""

    def scored(self, result):
        row = self.row(result)
        self.assertEqual(result["state"], "proposed")
        return row.confidence, row.level

    def test_a_creditor_reference_scores_1_and_goes_to_one_click(self):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.line.reference_number = "RF18 5390"
        self.assertEqual(self.scored(self.match()), (1.0, "1"))

    def test_a_payment_runs_end_to_end_id_scores_095_on_its_payment_entry(self):
        self.job.docs = []
        self.add("Payment Entry", "PE-9")
        self.job.payments = [
            {
                "doctype": "Payment Entry",
                "name": "PE-9",
                "party_name": "Invented Supplier AG",
                "direction": "out",
                "reference": "E2E-7",
                "amount": 500.0,
                "currency": "CHF",
                "date": "2027-02-07",
            }
        ]
        self.line.deposit, self.line.withdrawal = 0.0, 500.0
        self.line.end_to_end_id = "E2E-7"
        result = self.match(doctype="Payment Entry", name="PE-9")
        self.assertEqual(self.scored(result), (0.95, "1"))
        self.assertEqual(
            self.job.opened, [("payments", "Invented Co", None, USER, "PE-9")]
        )

    def test_the_party_within_five_days_scores_090(self):
        self.line.description = "Zahlung Invented Customer"
        self.assertEqual(self.scored(self.match()), (0.9, "1"))

    def test_the_party_within_thirty_days_scores_080_and_needs_an_approver(self):
        self.line.description = "Zahlung Invented Customer"
        self.job.docs = [open_invoice(date="2027-01-20")]  # 19 days before the line
        confidence, level = self.scored(self.match())
        self.assertEqual((confidence, level), (0.8, "2"))
        self.assertEqual(self.check(self.match(), "party within 5 days")["passed"], 0)

    def test_a_party_beyond_thirty_days_is_amount_only(self):
        self.line.description = "Zahlung Invented Customer"
        self.job.docs = [open_invoice(date="2026-12-01")]
        self.assertEqual(self.scored(self.match()), (0.6, "2"))

    def test_the_amount_alone_scores_060_and_needs_an_approver(self):
        self.assertEqual(self.scored(self.match()), (0.6, "2"))

    def test_the_model_score_can_only_lower_it(self):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.line.reference_number = "RF18 5390"
        self.assertEqual(self.scored(self.match(score=0.85)), (0.85, "2"))

    def test_an_amount_over_the_one_click_limit_goes_to_an_approver_whatever_the_proof(
        self,
    ):
        self.job.docs = [open_invoice(reference="RF185390", amount=1250.0)]
        self.line.deposit = 1250.0
        self.line.reference_number = "RF18 5390"
        confidence, level = self.scored(self.match())
        self.assertEqual((confidence, level), (1.0, "2"))
        self.assertEqual(self.row(self.match()).amount, 1250.0)

    def test_the_wider_party_pass_is_the_jobs_pass_with_a_wider_anchor(self):
        # the job's own pass still stops at five days
        from bi_finance import bank_match

        line = {
            "date": "2027-02-08",
            "deposit": 500.0,
            "withdrawal": 0.0,
            "currency": "CHF",
            "description": "Zahlung Invented Customer",
        }
        doc = open_invoice(date="2027-01-20")
        self.assertEqual(bank_match.by_party(line, [doc]), [])
        self.assertEqual(bank_match.by_party(line, [doc], anchor_days=30), [doc])


class MatchLedgerDate(MatchBase):
    def test_a_line_before_the_ledger_date_caps_at_half_and_lands_in_level_2(self):
        self.line.date = date(2026, 12, 30)
        result = self.match()
        self.assertEqual((result["state"], self.row(result).level), ("proposed", "2"))
        self.assertEqual(self.row(result).confidence, 0.5)
        self.assertEqual(self.check(result, "ledger date")["passed"], 0)

    def test_the_dry_run_before_the_ledger_date_writes_nothing_and_returns_the_entry_as_text(
        self,
    ):
        self.line.date = date(2026, 12, 30)
        proposed = self.match()
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="500.00")
        self.assertEqual((result["ok"], result["state"]), (True, "approved-dry-run"))
        text = self.row(result).dry_run_result
        for part in (
            "Receive",
            "Invented Customer AG",
            "SI-1",
            "500.00 CHF",
            "1100 Debtors",
            "1020 Bank",
            "2026-12-30",
        ):
            self.assertIn(part, text)
        self.assertEqual(self.applied(), ([], [], []))
        self.assertIsNone(self.row(result).result_name)

    def test_the_dry_run_of_a_bill_names_the_payment_the_other_way(self):
        self.add(
            "Purchase Invoice",
            "PI-1",
            supplier_name="Invented Supplier AG",
            credit_to="2000 Creditors",
        )
        self.job.docs = [
            open_invoice(
                doctype="Purchase Invoice",
                name="PI-1",
                direction="out",
                party_name="Invented Supplier AG",
            )
        ]
        self.line.deposit, self.line.withdrawal = 0.0, 500.0
        self.line.date = date(2026, 12, 30)
        proposed = self.match(doctype="Purchase Invoice", name="PI-1")
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="500.00")
        text = self.row(result).dry_run_result
        self.assertIn("Payment Entry (Pay) for Invented Supplier AG", text)
        self.assertIn("from 1020 Bank to 2000 Creditors", text)

    def test_the_dry_run_of_a_payment_entry_reconciles_without_a_new_entry(self):
        self.job.docs = []
        self.add("Payment Entry", "PE-9", party_name="Invented Supplier AG")
        self.job.payments = [
            {
                "doctype": "Payment Entry",
                "name": "PE-9",
                "party_name": "Invented Supplier AG",
                "direction": "in",
                "reference": "",
                "amount": 500.0,
                "currency": "CHF",
                "date": "2026-12-29",
            }
        ]
        self.line.date = date(2026, 12, 30)
        proposed = self.match(doctype="Payment Entry", name="PE-9")
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="500.00")
        self.assertIn(
            "Reconcile the bank line with the existing Payment Entry PE-9",
            self.row(result).dry_run_result,
        )
        self.assertEqual(self.job.booked, [])

    def test_after_the_ledger_date_the_approval_books_through_the_jobs_booking(self):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.line.reference_number = "RF18 5390"
        proposed = self.match()
        self.assertEqual(self.row(proposed).level, "1")
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        row = self.row(result)
        self.assertEqual(
            (row.result_doctype, row.result_name), ("Payment Entry", "PE-0001")
        )
        ((line, decision, gl_account),) = self.job.booked
        self.assertEqual(
            (line["name"], line["gl_account"], line["currency"]),
            ("BT-1", "1020 Bank", "CHF"),
        )
        self.assertEqual(decision["target"], ("Sales Invoice", "SI-1"))
        self.assertNotEqual(
            decision["pass"], "rule"
        )  # the job books a rule's line another way
        self.assertEqual(gl_account, "1020 Bank")

    def test_an_approval_a_day_later_refuses_a_line_that_was_reconciled_meanwhile(self):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.line.reference_number = "RF18 5390"
        proposed = self.match()
        self.line.status = "Reconciled"
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.job.booked, [])

    def test_a_failed_booking_ends_the_row_failed_and_rolls_back(self):
        self.job.docs = [open_invoice(reference="RF185390")]
        self.line.reference_number = "RF18 5390"
        proposed = self.match()

        def refuse(*args):
            raise ValueError("the bank refused")

        self.job._book = refuse
        result = actions.approve(proposed["name"], USER)
        self.assertEqual(result["state"], "failed")
        self.assertIn("the bank refused", self.row(result).error)
        self.assertTrue(self.frappe.rolled_back)


# ================================================================ submit_revaluation


class RevaluationBase(LedgerBase):
    def setUp(self):
        super().setUp()
        self.accounts = [
            Child(
                {
                    "account": "1050 Wise EUR",
                    "account_currency": "EUR",
                    "gain_loss": 40.0,
                }
            ),
            Child(
                {
                    "account": "1051 Wise USD",
                    "account_currency": "USD",
                    "gain_loss": -30.0,
                }
            ),
            Child(
                {"account": "1052 Wise GBP", "account_currency": "GBP", "gain_loss": 0}
            ),
        ]
        self.job.preview = [
            {"account": "1050 Wise EUR", "account_currency": "EUR", "gain_loss": 40.0},
            {"account": "1051 Wise USD", "account_currency": "USD", "gain_loss": -30.0},
        ]
        self.draft = self.add(
            "Exchange Rate Revaluation",
            "ERR-1",
            company="Invented Co",
            posting_date=date(2027, 1, 31),
            accounts=self.accounts,
        )

    def revalue(self, **kw):
        return self.propose(
            "submit_revaluation", {"exchange_rate_revaluation": "ERR-1"}, **kw
        )

    def refused(self, result, check):
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.check(result, check)["passed"], 0)


class RevaluationChecks(RevaluationBase):
    def test_a_draft_that_equals_the_preview_scores_1_and_goes_to_one_click(self):
        result = self.revalue()
        row = self.row(result)
        self.assertEqual(
            (result["state"], row.level, row.confidence), ("proposed", "1", 1.0)
        )

    def test_the_amount_is_the_absolute_total_gain_or_loss(self):
        # a gain of 40.00 and a loss of 30.00 net to a gain of 10.00
        self.assertEqual(self.row(self.revalue()).amount, 10.0)

    def test_a_total_over_the_one_click_limit_goes_to_an_approver(self):
        self.accounts[1]["gain_loss"] = -1500.0
        self.job.preview[1]["gain_loss"] = -1500.0
        row = self.row(self.revalue())
        self.assertEqual((row.level, row.amount), ("2", 1460.0))

    def test_a_draft_that_is_submitted_is_refused(self):
        self.draft.docstatus = 1
        self.refused(self.revalue(), "revaluation is a draft")

    def test_a_posting_date_in_mid_month_is_refused(self):
        self.draft.posting_date = date(2027, 1, 30)
        self.refused(self.revalue(), "month end")

    def test_every_month_end_is_one(self):
        for day in (
            date(2027, 2, 28),
            date(2028, 2, 29),
            date(2027, 4, 30),
            date(2027, 5, 31),
        ):
            self.draft.posting_date = day
            self.assertEqual(self.check(self.revalue(), "month end")["passed"], 1, day)

    def test_a_draft_a_rappen_off_the_fresh_preview_is_refused(self):
        self.job.preview[0]["gain_loss"] = 40.0 + 0.01  # a rappen more
        self.refused(self.revalue(), "equals a fresh preview")

    def test_a_draft_edited_by_hand_is_refused(self):
        self.accounts.append(
            Child(
                {
                    "account": "1053 Wise CAD",
                    "account_currency": "CAD",
                    "gain_loss": 1.0,
                }
            )
        )
        self.refused(self.revalue(), "equals a fresh preview")

    def test_a_preview_with_another_account_is_refused(self):
        self.job.preview[0]["account"] = "1059 Other"
        self.refused(self.revalue(), "equals a fresh preview")

    def test_a_row_without_a_gain_or_loss_is_no_part_of_either_side(self):
        self.job.preview.append(
            {"account": "1052 Wise GBP", "account_currency": "GBP", "gain_loss": 0}
        )
        self.assertEqual(
            self.check(self.revalue(), "equals a fresh preview")["passed"], 1
        )

    def test_a_draft_that_is_unreadable_is_refused(self):
        self.frappe.unreadable.add(("Exchange Rate Revaluation", "ERR-1"))
        self.refused(self.revalue(), "target readable")

    def test_the_year_end_needs_a_currency_exchange_row_per_currency(self):
        self.draft.posting_date = date(2026, 12, 31)
        self.frappe.matches["Currency Exchange"] = lambda filters: (
            ["CE-1"] if filters["from_currency"] == "EUR" else []
        )
        result = self.revalue()
        self.refused(result, "year-end rates")
        self.assertIn("USD", self.check(result, "year-end rates")["detail"])
        self.assertNotIn("EUR", self.check(result, "year-end rates")["detail"])

    def test_the_year_end_with_all_rates_passes_and_the_other_months_ask_for_none(self):
        self.draft.posting_date = date(2026, 12, 31)
        self.frappe.matches["Currency Exchange"] = lambda filters: ["CE-1"]
        self.assertEqual(self.check(self.revalue(), "year-end rates")["passed"], 1)
        self.draft.posting_date = date(2027, 1, 31)
        asked = len([r for r in self.frappe.reads if r[0] == "Currency Exchange"])
        self.revalue()
        self.assertEqual(
            len([r for r in self.frappe.reads if r[0] == "Currency Exchange"]), asked
        )


class RevaluationLedgerDate(RevaluationBase):
    def test_before_the_ledger_date_it_caps_at_half_and_the_approval_is_a_dry_run(self):
        self.draft.posting_date = date(2026, 12, 31)
        self.frappe.matches["Currency Exchange"] = lambda filters: ["CE-1"]
        proposed = self.revalue()
        self.assertEqual(
            (self.row(proposed).level, self.row(proposed).confidence), ("2", 0.5)
        )
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="10.00")
        self.assertEqual(result["state"], "approved-dry-run")
        self.assertIn(
            "Submit Exchange Rate Revaluation ERR-1 dated 2026-12-31 for 2 accounts",
            self.row(result).dry_run_result,
        )
        self.assertEqual(self.applied(), ([], [], []))

    def test_after_the_ledger_date_the_approval_submits_the_draft(self):
        proposed = self.revalue()
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        self.assertEqual(
            self.frappe.submitted, [("Exchange Rate Revaluation", "ERR-1")]
        )
        self.assertEqual(
            (self.row(result).result_doctype, self.row(result).result_name),
            ("Exchange Rate Revaluation", "ERR-1"),
        )

    def test_a_draft_whose_rates_moved_since_the_proposal_is_refused_at_the_approval(
        self,
    ):
        proposed = self.revalue()
        self.job.preview[0]["gain_loss"] = 45.0
        result = actions.approve(proposed["name"], USER)
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.frappe.submitted, [])


# ================================================================ submit_payment_proposal


class PaymentProposalBase(LedgerBase):
    def setUp(self):
        super().setUp()
        self.add("Account", "1020 Bank", account_currency="CHF", iban=IBAN)
        self.add("Purchase Invoice", "PI-1", iban=None)
        self.add("Supplier", "SUP-1", iban=IBAN)
        self.add("Employee", "EMP-1", bank_ac_no=IBAN)
        self.frappe.matches["Purchase Invoice"] = lambda filters: [
            Row({"name": "PI-1", "currency": "CHF"})
        ]
        self.frappe.matches["Expense Claim"] = lambda filters: [
            Row({"name": "EC-1", "currency": "CHF"})
        ]
        self.proposal = self.add(
            "Payment Proposal",
            "PP-1",
            date=date(2027, 2, 11),
            company="Invented Co",
            pay_from_account="1020 Bank",
            total=1500.0,
            purchase_invoices=[
                Child(
                    {
                        "purchase_invoice": "PI-1",
                        "supplier": "SUP-1",
                        "payment_type": "IBAN",
                        "currency": "CHF",
                    }
                )
            ],
            expenses=[Child({"expense_claim": "EC-1", "employee": "EMP-1"})],
            salaries=[],
        )

    def submit(self, **kw):
        return self.propose(
            "submit_payment_proposal", {"payment_proposal": "PP-1"}, **kw
        )

    def refused(self, result, check):
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.check(result, check)["passed"], 0)


class PaymentProposalChecks(PaymentProposalBase):
    def test_the_stub_iban_check_is_the_real_rule(self):
        self.assertTrue(iban_is_valid(IBAN))
        self.assertFalse(iban_is_valid(BAD_IBAN))

    def test_a_clean_proposal_is_level_2_whatever_the_confidence(self):
        result = self.submit(score=0.99)
        row = self.row(result)
        self.assertEqual(
            (result["state"], row.level, row.confidence), ("proposed", "2", 0.99)
        )
        self.assertEqual(
            row.amount, 1500.0
        )  # the proposal's total, the amount the approver types

    def test_a_proposal_that_is_submitted_is_refused(self):
        self.proposal.docstatus = 1
        self.refused(self.submit(), "proposal is a draft")

    def test_salary_rows_are_refused(self):
        self.proposal.salaries = [Row({"salary_slip": "SAL-1"})]
        self.refused(self.submit(), "bills and claims only")

    def test_a_proposal_with_nothing_to_pay_is_refused(self):
        self.proposal.purchase_invoices, self.proposal.expenses = [], []
        self.refused(self.submit(), "bills and claims only")

    def test_a_proposal_without_a_pay_from_account_is_refused(self):
        self.proposal.pay_from_account = None
        self.refused(self.submit(), "pay-from account")

    def test_a_supplier_iban_with_a_wrong_check_digit_is_refused(self):
        self.frappe.rows["Supplier"]["SUP-1"].iban = BAD_IBAN
        result = self.submit()
        self.refused(result, "IBANs valid")
        self.assertIn("bill row 1", self.check(result, "IBANs valid")["detail"])

    def test_a_supplier_without_an_iban_is_refused(self):
        self.frappe.rows["Supplier"]["SUP-1"].iban = None
        self.refused(self.submit(), "IBANs valid")

    def test_the_invoices_own_iban_wins_over_the_suppliers(self):
        self.frappe.rows["Purchase Invoice"]["PI-1"].iban = IBAN
        self.frappe.rows["Supplier"]["SUP-1"].iban = BAD_IBAN
        self.assertEqual(self.check(self.submit(), "IBANs valid")["passed"], 1)

    def test_an_esr_row_is_not_an_iban_row(self):
        self.proposal.purchase_invoices[0]["payment_type"] = "ESR"
        self.frappe.rows["Supplier"]["SUP-1"].iban = None
        self.assertEqual(self.check(self.submit(), "IBANs valid")["passed"], 1)

    def test_an_employees_account_that_fails_is_refused(self):
        self.frappe.rows["Employee"]["EMP-1"].bank_ac_no = BAD_IBAN
        result = self.submit()
        self.refused(result, "IBANs valid")
        self.assertIn("claim row 1", self.check(result, "IBANs valid")["detail"])

    def test_the_pay_from_accounts_iban_is_checked_too(self):
        self.frappe.rows["Account"]["1020 Bank"].iban = BAD_IBAN
        result = self.submit()
        self.refused(result, "IBANs valid")
        self.assertIn("pay-from account", self.check(result, "IBANs valid")["detail"])

    def test_a_bill_that_is_paid_meanwhile_is_refused(self):
        self.frappe.matches["Purchase Invoice"] = lambda filters: []
        self.refused(self.submit(), "no bill already paid")

    def test_a_claim_that_is_not_unpaid_is_refused(self):
        self.frappe.matches["Expense Claim"] = lambda filters: []
        self.refused(self.submit(), "no bill already paid")

    def test_a_bill_in_another_currency_than_the_account_is_refused(self):
        self.frappe.matches["Purchase Invoice"] = lambda filters: [
            Row({"name": "PI-1", "currency": "EUR"})
        ]
        self.refused(self.submit(), "currency of the account")

    def test_the_open_bills_are_read_in_one_query_each_bounded_by_the_rows(self):
        self.submit()
        bills = [r for r in self.frappe.reads if r[0] == "Purchase Invoice"]
        self.assertEqual(len(bills), 1)
        self.assertEqual((bills[0][1]["name"], bills[0][2]), (["in", ["PI-1"]], 1))

    def test_an_unreadable_proposal_is_refused(self):
        self.frappe.unreadable.add(("Payment Proposal", "PP-1"))
        self.refused(self.submit(), "target readable")


class PaymentProposalApproval(PaymentProposalBase):
    def test_the_requester_cannot_approve_while_another_approver_exists(self):
        proposed = self.submit()
        self.frappe.roles[USER] = ["AI Approver"]
        result = actions.approve(proposed["name"], USER, confirmed_amount="1500.00")
        self.assertFalse(result["ok"])
        self.assertEqual(self.frappe.submitted, [])

    def test_the_approval_submits_the_proposal_and_leaves_the_bank_file_alone(self):
        proposed = self.submit()
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="1500.00")
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        self.assertEqual(self.frappe.submitted, [("Payment Proposal", "PP-1")])
        self.assertEqual(
            (self.row(result).result_doctype, self.row(result).result_name),
            ("Payment Proposal", "PP-1"),
        )

    def test_a_wrong_typed_amount_submits_nothing(self):
        proposed = self.submit()
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="1400.00")
        self.assertFalse(result["ok"])
        self.assertEqual(self.frappe.submitted, [])

    def test_the_api_user_never_approves(self):
        proposed = self.submit()
        self.frappe.roles[API] = ["AI Approver"]
        self.switch(API)
        self.assertFalse(
            actions.approve(proposed["name"], API, confirmed_amount="1500.00")["ok"]
        )

    def test_before_the_ledger_date_the_approval_is_a_dry_run_that_submits_nothing(
        self,
    ):
        self.proposal.date = date(2026, 12, 30)
        proposed = self.submit()
        self.assertEqual(
            (self.row(proposed).level, self.row(proposed).confidence), ("2", 0.5)
        )
        self.assertEqual(self.check(proposed, "ledger date")["passed"], 0)
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="1500.00")
        self.assertEqual(result["state"], "approved-dry-run")
        text = self.row(result).dry_run_result
        self.assertIn("Submit Payment Proposal PP-1: 1 bills and 1 claims", text)
        self.assertIn("The bank file stays yours", text)
        self.assertEqual(self.applied(), ([], [], []))

    def test_the_bank_file_actions_stay_never(self):
        for name in ("download_bank_file", "upload_bank_file", "create_pain001"):
            self.assertEqual(self.propose(name, {})["state"], "rejected")


# ================================================================ submit_vat_declaration

FIELDS = {
    "total_revenue": "10000.00",
    "normal_amount_2024": "10000.00",
    "payable_tax": "810.00",
}


class VatBase(LedgerBase):
    def setUp(self):
        super().setUp()
        self.vat.fill_payment_basis = self.fill
        self.fills = []
        self.fresh = dict(FIELDS)
        self.draft = self.declaration(date(2027, 1, 1), date(2027, 3, 31))

    def fill(self, company, period, dry_run=0):
        self.fills.append((company, period, int(dry_run)))
        if self.vat.refuse:
            raise self.frappe.ValidationError(self.vat.refuse)
        return {"fields": dict(self.fresh)}

    def declaration(self, start, end, **over):
        values = {
            "company": "Invented Co",
            "start_date": start,
            "end_date": end,
            "total_revenue": 10000.0,
            "normal_amount_2024": 10000.0,
            "payable_tax": 810.0,
            **over,
        }
        return self.add("VAT Declaration", "VAT-1", **values)

    def submit(self, **kw):
        return self.propose(
            "submit_vat_declaration", {"vat_declaration": "VAT-1"}, **kw
        )

    def refused(self, result, check):
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.check(result, check)["passed"], 0)


class VatChecks(VatBase):
    def test_a_draft_that_equals_a_fresh_fill_is_level_2_whatever_the_confidence(self):
        result = self.submit(score=0.99)
        row = self.row(result)
        self.assertEqual(
            (result["state"], row.level, row.confidence), ("proposed", "2", 0.99)
        )
        self.assertEqual(row.amount, 810.0)
        self.assertEqual(
            self.fills, [("Invented Co", "2027Q1", 1)]
        )  # a dry run, never a write

    def test_a_submitted_declaration_is_refused(self):
        self.draft.docstatus = 1
        self.refused(self.submit(), "declaration is a draft")

    def test_dates_that_are_no_quarter_are_refused(self):
        self.draft.end_date = date(2027, 3, 30)
        self.refused(self.submit(), "period")

    def test_a_half_year_is_a_period_too(self):
        self.draft.start_date, self.draft.end_date = date(2027, 1, 1), date(2027, 6, 30)
        result = self.submit()
        self.assertEqual(
            self.check(result, "period")["detail"], "the declaration covers 2027H1"
        )
        self.assertEqual(self.fills[-1][1], "2027H1")

    def test_a_figure_a_rappen_off_the_fresh_fill_is_refused(self):
        self.draft.total_revenue = 10000.01
        result = self.submit()
        self.refused(result, "equals a fresh fill")
        self.assertIn(
            "total_revenue", self.check(result, "equals a fresh fill")["detail"]
        )

    def test_a_figure_the_fill_computes_that_the_draft_lacks_is_refused(self):
        self.fresh["tax_free_services"] = "250.00"
        self.refused(self.submit(), "equals a fresh fill")

    def test_a_fill_that_refuses_the_quarter_is_refused(self):
        self.vat.refuse = "The declaration of 2027Q1 is not filled: rate bases differ"
        result = self.submit()
        self.refused(result, "equals a fresh fill")
        self.assertIn(
            "rate bases differ", self.check(result, "equals a fresh fill")["detail"]
        )

    def test_figures_equal_to_the_rappen_whatever_the_float_noise(self):
        self.draft.payable_tax = 810.0000001
        self.assertEqual(self.check(self.submit(), "equals a fresh fill")["passed"], 1)

    def test_a_2027_quarter_is_not_compared_with_bexio(self):
        self.submit()
        self.assertEqual(
            [r for r in self.frappe.reads if r[0] == "MWST bexio Ziffer"], []
        )

    def test_an_unreadable_declaration_is_refused(self):
        self.frappe.unreadable.add(("VAT Declaration", "VAT-1"))
        self.refused(self.submit(), "target readable")


class VatAgainstBexio(VatBase):
    """For a 2026 quarter the MWST pro Quartal figures must equal bexio's (the report's own rows and difference)."""

    def setUp(self):
        super().setUp()
        self.draft = self.declaration(date(2026, 7, 1), date(2026, 9, 30))
        self.erp = {
            "302": [Decimal("10000.00"), Decimal("770.00")],
            "479": [Decimal("0"), Decimal("200.00")],
        }
        self.bexio = [
            Row({"ziffer": "302", "base": 10000.0, "tax": 770.0}),
            Row({"ziffer": "479", "base": 0.0, "tax": 200.0}),
        ]
        self.frappe.matches["MWST bexio Ziffer"] = lambda filters: self.bexio
        patcher = mock.patch.object(
            mwst_quarterly, "figures_of", lambda totals: self.erp
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_equal_figures_pass(self):
        result = self.submit()
        self.assertEqual(self.check(result, "equals bexio")["passed"], 1)
        self.assertEqual(self.fills, [("Invented Co", "2026Q3", 1)])

    def test_a_ziffer_that_differs_by_a_rappen_is_refused(self):
        self.bexio[0]["tax"] = 770.01
        self.erp["302"] = [Decimal("10000.00"), Decimal("770.00")]
        self.bexio[0]["base"] = 10000.01
        result = self.submit()
        self.refused(result, "equals bexio")
        self.assertIn(
            "1 Ziffern differ from bexio: 302",
            self.check(result, "equals bexio")["detail"],
        )

    def test_a_tax_ziffer_is_compared_on_its_tax(self):
        self.bexio[1]["tax"] = 199.99
        result = self.submit()
        self.refused(result, "equals bexio")
        self.assertIn("479", self.check(result, "equals bexio")["detail"])

    def test_a_ziffer_the_report_cannot_compute_yet_is_left_out(self):
        self.bexio.append(
            Row({"ziffer": "410", "base": 0.0, "tax": 55.0})
        )  # an open row of the report
        self.assertEqual(self.check(self.submit(), "equals bexio")["passed"], 1)

    def test_a_quarter_without_bexio_figures_cannot_be_checked_and_is_refused(self):
        self.bexio.clear()
        result = self.submit()
        self.refused(result, "equals bexio")
        self.assertIn(
            "bexio has no figures for 2026Q3",
            self.check(result, "equals bexio")["detail"],
        )

    def test_the_bexio_rows_are_read_for_the_one_quarter_with_a_limit(self):
        self.submit()
        ((_, filters, limit, _kw),) = (
            r for r in self.frappe.reads if r[0] == "MWST bexio Ziffer"
        )
        self.assertEqual(
            (filters, limit), ({"quarter": "2026Q3"}, ledger.BEXIO_ROWS_LIMIT)
        )

    def test_a_2026_half_year_is_not_a_quarter_and_not_compared(self):
        self.draft.start_date, self.draft.end_date = date(2026, 1, 1), date(2026, 6, 30)
        result = self.submit()
        self.assertEqual(
            [c for c in self.row(result).checks if c["check"] == "equals bexio"], []
        )
        self.assertEqual(
            [r for r in self.frappe.reads if r[0] == "MWST bexio Ziffer"], []
        )

    def test_the_period_end_is_the_ledger_date_so_a_2026_quarter_is_a_dry_run(self):
        proposed = self.submit()
        self.assertEqual(self.check(proposed, "ledger date")["passed"], 0)
        self.assertEqual(
            (self.row(proposed).level, self.row(proposed).confidence), ("2", 0.5)
        )
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="810.00")
        self.assertEqual(result["state"], "approved-dry-run")
        text = self.row(result).dry_run_result
        self.assertIn("Submit VAT Declaration VAT-1 for 2026-07-01 to 2026-09-30", text)
        self.assertIn("The transfer file and the ESTV upload stay yours", text)
        self.assertEqual(self.applied(), ([], [], []))

    def test_the_last_day_of_2026_is_still_before_the_ledger_date(self):
        self.draft.start_date, self.draft.end_date = date(2026, 10, 1), date(
            2026, 12, 31
        )
        self.assertEqual(self.check(self.submit(), "ledger date")["passed"], 0)


class VatApply(VatBase):
    def test_after_the_ledger_date_the_approval_submits_the_declaration(self):
        self.assertEqual(self.check(self.submit(), "ledger date")["passed"], 1)
        proposed = self.submit()
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="810.00")
        self.assertEqual((result["ok"], result["state"]), (True, "approved"))
        self.assertEqual(self.frappe.submitted, [("VAT Declaration", "VAT-1")])
        self.assertEqual(
            (self.row(result).result_doctype, self.row(result).result_name),
            ("VAT Declaration", "VAT-1"),
        )

    def test_a_declaration_whose_figures_moved_after_the_proposal_is_refused_at_the_approval(
        self,
    ):
        proposed = self.submit()
        self.fresh["payable_tax"] = "820.00"
        self.switch(OTHER)
        result = actions.approve(proposed["name"], OTHER, confirmed_amount="810.00")
        self.assertEqual((result["ok"], result["state"]), (False, "rejected"))
        self.assertEqual(self.frappe.submitted, [])

    def test_the_typed_amount_is_the_payable_tax_to_the_rappen(self):
        proposed = self.submit()
        self.switch(OTHER)
        self.assertFalse(
            actions.approve(proposed["name"], OTHER, confirmed_amount="810.01")["ok"]
        )
        self.assertEqual(self.frappe.submitted, [])

    def test_the_transfer_file_and_the_estv_upload_stay_never(self):
        for name in ("upload_estv", "create_pain001"):
            self.assertEqual(self.propose(name, {})["state"], "rejected")


# ================================================================ the registry


class Registry(unittest.TestCase):
    def test_the_four_types_are_registered_with_their_level_class_and_a_ledger_effect(
        self,
    ):
        expected = {
            "match_bank_transaction": levels.ONE_CLICK,
            "submit_revaluation": levels.ONE_CLICK,
            "submit_payment_proposal": levels.ALWAYS_APPROVER,
            "submit_vat_declaration": levels.ALWAYS_APPROVER,
        }
        for name, level_class in expected.items():
            spec = actions.ACTIONS[name]
            self.assertEqual(
                (spec.level_class, spec.ledger_effect), (level_class, True), name
            )

    def test_the_level_0_types_are_still_there(self):
        for name in (
            "add_comment",
            "create_todo",
            "close_todo",
            "create_payment_proposal",
            "create_vat_draft",
        ):
            self.assertIn(name, actions.ACTIONS)

    def test_each_schema_names_its_arguments_and_no_other(self):
        for name in expected_arguments():
            schema = actions.ACTIONS[name].schema
            self.assertEqual(
                sorted(schema["properties"]), sorted(expected_arguments()[name])
            )
            self.assertEqual(
                sorted(schema["required"]), sorted(expected_arguments()[name])
            )
            self.assertIs(schema["additionalProperties"], False)

    def test_no_date_helper_reads_a_missing_document_as_a_date(self):
        self.assertIsNone(ledger._date(None))
        self.assertEqual(
            ledger._date("2027-01-31 00:00:00"), datetime.date(2027, 1, 31)
        )

    def test_figures_compare_in_rappen_without_float_noise(self):
        self.assertEqual(ledger._cents(0.1 + 0.2), 30)
        self.assertEqual(ledger._cents("12.005"), 1201)
        self.assertEqual(ledger._cents(None), 0)
        self.assertEqual(ledger._cents(-17.5), -1750)


def expected_arguments():
    return {
        "match_bank_transaction": ["bank_transaction", "doctype", "name"],
        "submit_revaluation": ["exchange_rate_revaluation"],
        "submit_payment_proposal": ["payment_proposal"],
        "submit_vat_declaration": ["vat_declaration"],
    }


if __name__ == "__main__":
    unittest.main()
