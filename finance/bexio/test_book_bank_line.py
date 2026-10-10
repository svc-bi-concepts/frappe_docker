"""Offline tests for book_bank_line.py. Invented lines, merchants and accounts only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import copy
import io
import unittest
from decimal import Decimal

import book_bank_line as bb
import import_master as im

BANK_GL = "1021 - Testbank - X"
EXPENSE = "6500 - Testaufwand - X"
OTHER_EXPENSE = "6510 - Anderer Aufwand - X"
VAT = "1170 - Vorsteuer - X"
TAX_ACCOUNTS = {VAT}

LINE = {"name": "ACC-BTN-TEST-0001", "docstatus": 1, "date": "2026-09-07", "description": "Card transaction of 40.00 EUR issued by Testshop Alpha 07-09-2026",
        "bank_account": "Hauptkonto - Testbank AG", "status": "Unreconciled", "withdrawal": 40.0, "deposit": 0.0, "payment_entries": []}

# the earlier lines of the same merchant, and one of another merchant; their vouchers are Journal Entries
EARLIER = [
    {"name": "ACC-BTN-TEST-0002", "description": "Card transaction of 12.00 EUR issued by Testshop Alpha 01-08-2026", "status": "Reconciled", "docstatus": 1},
    {"name": "ACC-BTN-TEST-0003", "description": "Card transaction of 9.50 EUR issued by Testshop Alpha 03-08-2026", "status": "Reconciled", "docstatus": 1},
    {"name": "ACC-BTN-TEST-0004", "description": "Card transaction of 9.50 EUR issued by Other Beta 04-08-2026", "status": "Reconciled", "docstatus": 1},
    {"name": "ACC-BTN-TEST-0005", "description": "Card transaction of 5.00 EUR issued by Testshop Alpha 05-08-2026", "status": "Unreconciled", "docstatus": 1},
]


def gl_rows(voucher, expense, amount, vat=None):
    """The GL rows of a Journal Entry that paid one expense from the bank: the expense, optional VAT, the bank's credit."""
    rows = [{"voucher_type": "Journal Entry", "voucher_no": voucher, "account": expense, "debit": amount, "credit": 0}]
    if vat:
        rows.append({"voucher_type": "Journal Entry", "voucher_no": voucher, "account": VAT, "debit": vat, "credit": 0})
    rows.append({"voucher_type": "Journal Entry", "voucher_no": voucher, "account": BANK_GL, "debit": 0,
                 "credit": amount + (vat or 0)})
    return rows


class FakeErp:
    """The reads and writes the command makes, over invented documents. It records each write."""

    def __init__(self, earlier, allocations, gl, line=None, journal=None):
        self.line = copy.deepcopy(line or LINE)
        self.earlier = earlier
        self.allocations = allocations
        self.gl = gl
        self.journal = journal or {}
        self.writes = []

    def list(self, doctype, filters=None, fields=("name",)):
        if doctype == "Bank Account":
            return [{"name": "Hauptkonto - Testbank AG", "account": BANK_GL}]
        if doctype == "Bank Transaction":
            return [dict(e) for e in self.earlier]
        if doctype == "GL Entry":
            return [dict(r) for r in self.gl]
        if doctype == "Account":
            return [{"name": a} for a in TAX_ACCOUNTS]
        if doctype == "Journal Entry":
            key = filters[0][2]
            return [{"name": j["name"], "docstatus": j["docstatus"]} for j in self.journal.values() if j["bexio_id"] == key]
        raise AssertionError(doctype)

    def get(self, doctype, name):
        if name == self.line["name"]:
            return copy.deepcopy(self.line)
        return {"payment_entries": copy.deepcopy(self.allocations.get(name, []))}

    def insert(self, doctype, doc):
        self.writes.append(("insert", doctype))
        name = "JE-{}".format(len(self.journal) + 1)
        stored = dict(doc, name=name, docstatus=0)
        self.journal[name] = stored
        return dict(stored)

    def call(self, method, **args):
        self.writes.append(("call", method))
        if method == "frappe.client.submit":
            stored = self.journal[args["doc"]["name"]]
            stored["docstatus"] = 1
            return dict(stored)
        if method == bb.RECONCILE:
            self.line["status"] = "Reconciled"
            self.line["payment_entries"] = [{"payment_document": "Journal Entry", "payment_entry": json_name(args["vouchers"])}]
            return None
        raise AssertionError(method)


def json_name(vouchers):
    import json
    return json.loads(vouchers)[0]["payment_name"]


def history_erp(merchant_expense=EXPENSE, vat=None, extra=None):
    """The earlier Testshop Alpha lines booked to one expense account, the first two with a reconciled Journal Entry."""
    allocations = {"ACC-BTN-TEST-0002": [{"payment_document": "Journal Entry", "payment_entry": "JE-OLD-1"}],
                   "ACC-BTN-TEST-0003": [{"payment_document": "Journal Entry", "payment_entry": "JE-OLD-2"}],
                   "ACC-BTN-TEST-0004": [{"payment_document": "Journal Entry", "payment_entry": "JE-OLD-3"}]}
    gl = gl_rows("JE-OLD-1", merchant_expense, 12, vat) + gl_rows("JE-OLD-2", merchant_expense, 9.5, vat) + \
        gl_rows("JE-OLD-3", OTHER_EXPENSE, 9.5)
    gl += extra or []
    return FakeErp(EARLIER, allocations, gl)


class MerchantWordsTest(unittest.TestCase):
    def test_words_are_the_text_after_the_amount_less_the_date_and_reference(self):
        self.assertEqual(bb.merchant_words("Card transaction of 40.00 EUR issued by Testshop Alpha 07-09-2026"),
                         ["issued", "by", "testshop", "alpha"])
        self.assertEqual(bb.merchant_words("Card transaction of 9 USD Gamma Store Ref 12345"), ["gamma", "store"])

    def test_a_description_that_is_no_card_line_has_no_words(self):
        self.assertIsNone(bb.merchant_words("Transfer to supplier 2026-09-07"))
        self.assertIsNone(bb.merchant_words(""))

    def test_the_merchant_matches_as_whole_words_in_order(self):
        words = ["issued", "by", "testshop", "alpha"]
        self.assertTrue(bb.names_merchant("Card transaction of 1.00 EUR issued by Testshop Alpha 01-08-2026", words))
        self.assertFalse(bb.names_merchant("Card transaction of 1.00 EUR issued by Testshop Alphabet", words))
        self.assertFalse(bb.names_merchant("Card transaction of 1.00 EUR issued by Alpha Testshop", words))


class TreatmentTest(unittest.TestCase):
    def history(self, *vouchers):
        return [{"vouchers": list(vouchers)}]

    def test_one_expense_account_without_vat_is_the_treatment(self):
        account, tax, rate = bb.treatment(self.history([(EXPENSE, Decimal("12")), (BANK_GL, Decimal("-12"))]), BANK_GL, TAX_ACCOUNTS)
        self.assertEqual((account, tax, rate), (EXPENSE, None, None))

    def test_one_expense_account_with_vat_gives_the_rate_of_that_account(self):
        rows = [(EXPENSE, Decimal("100")), (VAT, Decimal("8.10")), (BANK_GL, Decimal("-108.10"))]
        account, tax, rate = bb.treatment(self.history(rows), BANK_GL, TAX_ACCOUNTS)
        self.assertEqual((account, tax, rate), (EXPENSE, VAT, Decimal("0.081")))

    def test_two_expense_accounts_need_benchi_and_name_the_numbers(self):
        rows = [(EXPENSE, Decimal("12")), (OTHER_EXPENSE, Decimal("9")), (BANK_GL, Decimal("-21"))]
        with self.assertRaises(bb.Refused) as caught:
            bb.treatment(self.history(rows), BANK_GL, TAX_ACCOUNTS)
        self.assertIn("2 distinct expense accounts", str(caught.exception))
        self.assertIn("6500, 6510", str(caught.exception))

    def test_no_history_needs_benchi(self):
        with self.assertRaises(bb.Refused) as caught:
            bb.treatment([], BANK_GL, TAX_ACCOUNTS)
        self.assertIn("0 distinct expense accounts", str(caught.exception))

    def test_vat_and_no_vat_in_the_history_need_benchi(self):
        plain = [(EXPENSE, Decimal("12")), (BANK_GL, Decimal("-12"))]
        taxed = [(EXPENSE, Decimal("100")), (VAT, Decimal("8.10")), (BANK_GL, Decimal("-108.10"))]
        with self.assertRaises(bb.Refused) as caught:
            bb.treatment([{"vouchers": [plain]}, {"vouchers": [taxed]}], BANK_GL, TAX_ACCOUNTS)
        self.assertIn("2 VAT treatments", str(caught.exception))


class HistoryTest(unittest.TestCase):
    def test_a_cancelled_line_is_no_booking_and_does_not_count(self):
        erp = history_erp()
        erp.earlier = erp.earlier + [{"name": "ACC-BTN-TEST-0006", "docstatus": 2, "status": "Cancelled",
                                      "description": "Card transaction of 7.00 EUR issued by Testshop Alpha 06-08-2026"}]
        erp.allocations["ACC-BTN-TEST-0006"] = [{"payment_document": "Journal Entry", "payment_entry": "JE-OLD-4"}]
        erp.gl += gl_rows("JE-OLD-4", OTHER_EXPENSE, 7)
        plan, counts = bb.decide(erp, erp.line, BANK_GL)
        self.assertEqual(plan["account"], EXPENSE)
        self.assertEqual(counts, {"earlier lines": 3, "with a reconciled voucher": 2})


class PlanTest(unittest.TestCase):
    def test_without_vat_the_expense_is_the_gross_amount(self):
        plan = bb.plan_for(Decimal("40.00"), EXPENSE, None, None)
        self.assertEqual((plan["net"], plan["tax"]), (Decimal("40.00"), Decimal("0")))

    def test_with_vat_the_net_and_tax_add_up_to_the_gross(self):
        plan = bb.plan_for(Decimal("108.10"), EXPENSE, VAT, Decimal("0.081"))
        self.assertEqual((plan["net"], plan["tax"]), (Decimal("100.00"), Decimal("8.10")))
        self.assertEqual(plan["net"] + plan["tax"], Decimal("108.10"))

    def test_the_journal_entry_balances_and_is_keyed_by_the_line(self):
        plan = bb.plan_for(Decimal("108.10"), EXPENSE, VAT, Decimal("0.081"))
        plan["bank_gl"] = BANK_GL
        doc = bb.journal_entry(dict(LINE, withdrawal=108.10), BANK_GL, plan)
        debit = sum(r.get("debit", 0) for r in doc["accounts"])
        credit = sum(r.get("credit", 0) for r in doc["accounts"])
        self.assertEqual(debit, credit)
        self.assertEqual(doc["bexio_id"], "book-ACC-BTN-TEST-0001")
        self.assertEqual(doc["posting_date"], "2026-09-07")
        self.assertEqual(doc["company"], im.COMPANY)
        self.assertEqual(doc["user_remark"], "Wise card payment booked from history, line ACC-BTN-TEST-0001")
        self.assertEqual([r["account"] for r in doc["accounts"]], [EXPENSE, VAT, BANK_GL])


class BookTest(unittest.TestCase):
    def run_book(self, erp, apply):
        with contextlib.redirect_stdout(io.StringIO()):
            return bb.book(erp, "ACC-BTN-TEST-0001", apply)

    def test_a_dry_run_plans_and_writes_nothing(self):
        erp = history_erp()
        lines, plan = self.run_book(erp, apply=False)
        self.assertEqual(plan["account"], EXPENSE)
        self.assertIn("dry run: nothing was written to ERPNext", lines)
        self.assertEqual(erp.writes, [])

    def test_apply_books_one_journal_entry_and_reconciles_the_line(self):
        erp = history_erp()
        lines, _ = self.run_book(erp, apply=True)
        self.assertEqual(erp.writes, [("insert", "Journal Entry"), ("call", "frappe.client.submit"), ("call", bb.RECONCILE)])
        self.assertEqual(erp.line["status"], "Reconciled")
        self.assertEqual(list(erp.journal.values())[0]["docstatus"], 1)
        self.assertTrue(any("reconciled" in line for line in lines))

    def test_a_rerun_after_apply_does_nothing(self):
        erp = history_erp()
        self.run_book(erp, apply=True)
        erp.writes.clear()
        lines, plan = self.run_book(erp, apply=True)
        self.assertEqual(erp.writes, [])
        self.assertIsNone(plan)
        self.assertIn("already Reconciled, nothing to do", lines[0])

    def test_a_journal_entry_left_by_an_earlier_run_is_reconciled_not_booked_twice(self):
        erp = history_erp()
        erp.journal = {"JE-9": {"name": "JE-9", "bexio_id": "book-ACC-BTN-TEST-0001", "docstatus": 1}}
        self.run_book(erp, apply=True)
        self.assertEqual(erp.writes, [("call", bb.RECONCILE)])
        self.assertEqual(erp.line["status"], "Reconciled")

    def test_no_history_books_nothing_and_says_so(self):
        erp = FakeErp([], {}, [])
        with self.assertRaises(bb.Refused):
            self.run_book(erp, apply=True)
        self.assertEqual(erp.writes, [])

    def test_a_line_that_is_not_a_withdrawal_is_refused(self):
        erp = history_erp()
        erp.line["deposit"], erp.line["withdrawal"] = 40.0, 0.0
        with self.assertRaises(bb.Refused):
            self.run_book(erp, apply=True)
        self.assertEqual(erp.writes, [])


if __name__ == "__main__":
    unittest.main()
