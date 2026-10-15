"""Tests of bank_match: which line pays which document. Invented figures and names only; no site, pure Python.

    python3 -m unittest bi_finance.test_bank_match

from this app's directory. Part of the depot's light gate (.yardr/check).
"""

import unittest

from bi_finance import bank_match, bank_rules
from bi_finance.reference import creditor_reference


def line(
    name,
    amount,
    description="",
    date="2026-09-10",
    reference=None,
    transaction_id=None,
    currency="CHF",
    end_to_end=None,
):
    deposit = amount if amount > 0 else 0
    withdrawal = -amount if amount < 0 else 0
    return {
        "name": name,
        "date": date,
        "deposit": deposit,
        "withdrawal": withdrawal,
        "description": description,
        "reference_number": reference,
        "transaction_id": transaction_id,
        "end_to_end_id": end_to_end,
        "currency": currency,
        "company": "Test Co",
        "bank_account": "Test Bank CHF",
    }


def invoice(
    doctype,
    name,
    amount,
    party_name="Beispiel Holding AG",
    date="2026-09-08",
    currency="CHF",
):
    direction = "in" if doctype == "Sales Invoice" else "out"
    reference = None
    if doctype == "Sales Invoice":
        reference = bank_match.sales_invoice_reference(name)
    return {
        "doctype": doctype,
        "name": name,
        "party_name": party_name,
        "direction": direction,
        "amount": amount,
        "currency": currency,
        "date": date,
        "reference": reference,
    }


def payment(name, amount, reference, date="2026-09-09", currency="CHF"):
    return {
        "doctype": "Payment Entry",
        "name": name,
        "reference": bank_match.key(reference),
        "direction": "out",
        "amount": amount,
        "currency": currency,
        "date": date,
        "party_name": "Muster Lieferant GmbH",
    }


def rule(
    name,
    priority=10,
    transaction_type="Withdrawal",
    account="6900 - Bankgebühren",
    checks=(),
    **extra
):
    return dict(
        {
            "name": name,
            "priority": priority,
            "company": "Test Co",
            "transaction_type": transaction_type,
            "classify_as": "Bank Entry",
            "bank_entry_type": "Single Account",
            "account": account,
            "description_rules": [
                {"check": check, "value": value} for check, value in checks
            ],
        },
        **extra,
    )


def state_of(decisions, name):
    return next(d for d in decisions if d["line"] == name)


class ReferenceTest(unittest.TestCase):
    def test_reference_of_invoice_reads_back_from_the_line(self):
        ref = bank_match.sales_invoice_reference("ACC-SINV-2026-00001")
        self.assertEqual(ref, bank_match.key(creditor_reference("ACC-SINV-2026-00001")))
        self.assertEqual(bank_match.key("RF38 ACCS INV2 0260 0001"), ref)

    def test_a_number_that_gives_no_reference_has_none(self):
        self.assertIsNone(bank_match.sales_invoice_reference("A" * 22))


class FitsTest(unittest.TestCase):
    def test_direction_currency_and_amount_must_all_agree(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00001", 100.0)
        self.assertTrue(bank_match.fits(line("L1", 100.0), inv))
        self.assertFalse(bank_match.fits(line("L1", -100.0), inv))
        self.assertFalse(bank_match.fits(line("L1", 99.99), inv))
        self.assertFalse(bank_match.fits(line("L1", 100.0, currency="EUR"), inv))

    def test_the_cent_is_compared_not_a_rounding_of_floats(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00002", 0.3)
        self.assertTrue(bank_match.fits(line("L1", 0.1 + 0.2), inv))


class PassesTest(unittest.TestCase):
    def test_reference_names_the_invoice(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00001", 250.0)
        ref = creditor_reference("ACC-SINV-2026-00001")
        decisions = bank_match.decide([line("L1", 250.0, reference=ref)], [inv], [], [])
        self.assertEqual(decisions[0]["state"], "match")
        self.assertEqual(decisions[0]["pass"], "reference")
        self.assertEqual(
            decisions[0]["target"], ("Sales Invoice", "ACC-SINV-2026-00001")
        )

    def test_reference_of_a_wrong_amount_does_not_match(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00001", 250.0)
        ref = creditor_reference("ACC-SINV-2026-00001")
        decisions = bank_match.decide([line("L1", 240.0, reference=ref)], [inv], [], [])
        self.assertEqual(decisions[0]["state"], "none")

    def test_payment_reference_names_the_payment_entry(self):
        pay = payment("PE-0001", 80.0, "PINV-0007")
        decisions = bank_match.decide(
            [line("L1", -80.0, reference="PINV-0007")], [], [pay], []
        )
        self.assertEqual(decisions[0]["pass"], "payment_reference")
        self.assertEqual(decisions[0]["target"], ("Payment Entry", "PE-0001"))

    def test_end_to_end_id_names_the_payment_before_the_reference_number(self):
        by_end_to_end = payment("PE-0002", 80.0, "PAY-0042")
        by_reference = payment("PE-0003", 80.0, "PINV-0008")
        decisions = bank_match.decide(
            [line("L1", -80.0, reference="PINV-0008", end_to_end="PAY-0042")],
            [],
            [by_reference, by_end_to_end],
            [],
        )
        self.assertEqual(decisions[0]["pass"], "payment_reference")
        self.assertEqual(decisions[0]["target"], ("Payment Entry", "PE-0002"))

    def test_reference_number_names_the_payment_when_no_end_to_end_id_fits(self):
        pay = payment("PE-0004", 80.0, "PINV-0009")
        decisions = bank_match.decide(
            [line("L1", -80.0, reference="PINV-0009", end_to_end="PAY-9999")],
            [],
            [pay],
            [],
        )
        self.assertEqual(decisions[0]["target"], ("Payment Entry", "PE-0004"))

    def test_end_to_end_id_of_a_payment_of_another_amount_does_not_match(self):
        pay = payment("PE-0005", 79.0, "PAY-0043")
        decisions = bank_match.decide(
            [line("L1", -80.0, end_to_end="PAY-0043")], [], [pay], []
        )
        self.assertEqual(decisions[0]["state"], "none")

    def test_party_within_the_anchor_is_matched_when_only_one_fits(self):
        inv = invoice(
            "Sales Invoice",
            "ACC-SINV-2026-00003",
            120.0,
            party_name="Beispiel Holding AG",
            date="2026-09-08",
        )
        decisions = bank_match.decide(
            [
                line(
                    "L1",
                    120.0,
                    description="Gutschrift Beispiel Holding",
                    date="2026-09-12",
                )
            ],
            [inv],
            [],
            [],
        )
        self.assertEqual(decisions[0]["pass"], "party")

    def test_party_outside_the_anchor_stays(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00003", 120.0, date="2026-09-01")
        decisions = bank_match.decide(
            [line("L1", 120.0, description="Beispiel Holding", date="2026-09-12")],
            [inv],
            [],
            [],
        )
        self.assertEqual(decisions[0]["state"], "none")

    def test_party_with_two_invoices_of_the_amount_stays(self):
        first = invoice("Sales Invoice", "ACC-SINV-2026-00004", 120.0)
        second = invoice("Sales Invoice", "ACC-SINV-2026-00005", 120.0)
        decisions = bank_match.decide(
            [line("L1", 120.0, description="Beispiel Holding")], [first, second], [], []
        )
        self.assertEqual(decisions[0]["state"], "uncertain")
        self.assertEqual(decisions[0]["reason"], bank_match.SEVERAL)

    def test_a_generic_description_names_no_party(self):
        inv = invoice(
            "Sales Invoice",
            "ACC-SINV-2026-00006",
            120.0,
            party_name="Beispiel Holding AG",
        )
        decisions = bank_match.decide(
            [line("L1", 120.0, description="Transfer AG")], [inv], [], []
        )
        self.assertEqual(decisions[0]["state"], "none")

    def test_bills_match_outgoing_lines_only(self):
        bill = invoice(
            "Purchase Invoice",
            "ACC-PINV-2026-00001",
            60.0,
            party_name="Muster Lieferant GmbH",
        )
        decisions = bank_match.decide(
            [line("L1", -60.0, description="Muster Lieferant", date="2026-09-11")],
            [bill],
            [],
            [],
        )
        self.assertEqual(decisions[0]["pass"], "party")
        self.assertEqual(
            decisions[0]["target"], ("Purchase Invoice", "ACC-PINV-2026-00001")
        )
        incoming = bank_match.decide(
            [line("L2", 60.0, description="Muster Lieferant", date="2026-09-11")],
            [bill],
            [],
            [],
        )
        self.assertEqual(incoming[0]["state"], "none")


class BothSidesTest(unittest.TestCase):
    def test_a_document_two_lines_name_is_certain_for_neither(self):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00007", 300.0)
        ref = creditor_reference("ACC-SINV-2026-00007")
        lines = [
            line("L1", 300.0, reference=ref),
            line("L2", 300.0, reference=ref, date="2026-09-11"),
        ]
        decisions = bank_match.decide(lines, [inv], [], [])
        self.assertEqual([d["state"] for d in decisions], ["uncertain", "uncertain"])
        self.assertEqual({d["reason"] for d in decisions}, {bank_match.SHARED})

    def test_a_line_that_names_a_document_shared_with_another_stays_even_by_another_pass(
        self,
    ):
        inv = invoice("Sales Invoice", "ACC-SINV-2026-00008", 300.0)
        ref = creditor_reference("ACC-SINV-2026-00008")
        lines = [
            line("L1", 300.0, reference=ref),
            line("L2", 300.0, description="Beispiel Holding", date="2026-09-11"),
        ]
        decisions = bank_match.decide(lines, [inv], [], [])
        self.assertEqual(state_of(decisions, "L1")["state"], "uncertain")
        self.assertEqual(state_of(decisions, "L2")["state"], "uncertain")


class RuleTest(unittest.TestCase):
    def test_a_fee_line_takes_the_rule_of_its_text(self):
        fee = rule("fees", checks=[("Contains", "wise fee")])
        decisions = bank_match.decide(
            [line("L1", -4.5, description="Wise fee for card transaction")],
            [],
            [],
            [fee],
        )
        self.assertEqual(decisions[0]["state"], "match")
        self.assertEqual(decisions[0]["pass"], "rule")
        self.assertEqual(decisions[0]["target"], "fees")

    def test_the_first_rule_by_priority_wins(self):
        late = rule("late", priority=20, checks=[("Contains", "fee")])
        early = rule("early", priority=5, checks=[("Contains", "fee")])
        self.assertEqual(
            bank_match.rule_for(line("L1", -4.5, "fee"), [late, early])[0]["name"],
            "early",
        )

    def test_the_test_mirrors_erpnext_for_each_check(self):
        text = "Interest credit Q3"
        for check, value, expected in [
            ("Contains", "credit", True),
            ("Starts With", "Interest", True),
            ("Starts With", "credit", False),
            ("Ends With", "Q3", True),
            ("Regex", r"Q\d$", True),
            ("Regex", r"Q[a-z]$", False),
        ]:
            with self.subTest(check=check, value=value):
                r = rule("r", transaction_type="Deposit", checks=[(check, value)])
                self.assertEqual(
                    bank_match.rule_matches(line("L1", 2.0, text), r), expected
                )

    def test_a_rule_with_no_description_condition_never_matches(self):
        self.assertFalse(
            bank_match.rule_matches(line("L1", -4.5, "anything"), rule("r", checks=[]))
        )

    def test_type_and_amount_limits_are_read(self):
        r = rule(
            "r", transaction_type="Deposit", checks=[("Contains", "x")], min_amount=10
        )
        self.assertFalse(bank_match.rule_matches(line("L1", -20.0, "x"), r))
        self.assertFalse(bank_match.rule_matches(line("L2", 5.0, "x"), r))
        self.assertTrue(bank_match.rule_matches(line("L3", 20.0, "x"), r))

    def test_a_rule_of_another_company_does_not_take_the_line(self):
        r = rule("r", checks=[("Contains", "fee")], company="Other Co")
        self.assertFalse(bank_match.rule_matches(line("L1", -4.5, "fee"), r))

    def test_a_first_match_that_is_not_a_single_account_bank_entry_stays(self):
        payment_rule = rule(
            "pe", classify_as="Payment Entry", checks=[("Contains", "fee")]
        )
        rule_obj, reason = bank_match.rule_for(line("L1", -4.5, "fee"), [payment_rule])
        self.assertIsNone(rule_obj)
        self.assertIsNotNone(reason)
        decisions = bank_match.decide([line("L1", -4.5, "fee")], [], [], [payment_rule])
        self.assertEqual(
            (decisions[0]["state"], decisions[0]["reason"]),
            ("uncertain", bank_match.NOT_BOOKED),
        )

    def test_a_document_match_is_not_overridden_by_a_rule(self):
        inv = invoice(
            "Sales Invoice",
            "ACC-SINV-2026-00009",
            50.0,
            party_name="Beispiel Holding AG",
        )
        ref = creditor_reference("ACC-SINV-2026-00009")
        fee = rule(
            "fees", transaction_type="Deposit", checks=[("Contains", "beispiel")]
        )
        decisions = bank_match.decide(
            [line("L1", 50.0, "Beispiel", reference=ref)], [inv], [], [fee]
        )
        self.assertEqual(decisions[0]["pass"], "reference")


class SeedRulesTest(unittest.TestCase):
    """bank_rules.RULES as the patch makes them, against the texts the feed and the bank write (invented amounts)."""

    def rules(self):
        return [
            rule(
                spec["rule_name"],
                transaction_type=spec["transaction_type"],
                checks=[("Contains", text) for text in spec["texts"]],
            )
            for spec in bank_rules.RULES
        ]

    def taken_by(self, amount, description):
        found = bank_match.rule_for(line("L1", amount, description), self.rules())[0]
        return found["name"] if found else None

    def test_a_payout_fee_row_takes_the_wise_fee_rule(self):
        self.assertEqual(
            self.taken_by(-4.5, "Wise fee: Ihre Überweisung 42"), "Wise fees"
        )

    def test_bank_fees_take_the_ubs_fee_rule(self):
        self.assertEqual(self.taken_by(-12.0, "Kontoführung Q3"), "UBS bank fees")

    def test_interest_takes_the_rule_of_its_direction(self):
        self.assertEqual(self.taken_by(2.1, "Zins Guthaben"), "UBS interest received")
        self.assertEqual(self.taken_by(-7.4, "Zins Kredit"), "UBS interest paid")

    def test_a_line_of_no_rule_stays(self):
        self.assertIsNone(self.taken_by(-120.0, "Miete Testlokal"))

    def test_each_rule_has_an_account_number_and_texts(self):
        for spec in bank_rules.RULES:
            with self.subTest(rule=spec["rule_name"]):
                self.assertTrue(spec["account_number"].isdigit())
                self.assertTrue(spec["texts"])


class SummaryTest(unittest.TestCase):
    def test_counts_per_pass_and_month_without_names(self):
        inv = invoice(
            "Sales Invoice",
            "ACC-SINV-2026-00010",
            70.0,
            party_name="Beispiel Holding AG",
        )
        ref = creditor_reference("ACC-SINV-2026-00010")
        fee = rule("fees", checks=[("Contains", "fee")])
        lines = [
            line("L1", 70.0, reference=ref, date="2026-08-03"),
            line("L2", -4.5, "fee", date="2026-08-04"),
            line("L3", -9.0, "unknown text", date="2026-09-05"),
        ]
        out = bank_match.summary(bank_match.decide(lines, [inv], [], [fee]))
        self.assertEqual(
            (out["lines"], out["match"], out["uncertain"], out["none"]), (3, 2, 0, 1)
        )
        self.assertEqual(
            out["by_pass"],
            {"reference": 1, "payment_reference": 0, "party": 0, "rule": 1},
        )
        self.assertEqual(out["by_month"], {"2026-08 reference": 1, "2026-08 rule": 1})
        self.assertNotIn("L1", repr(out))
        self.assertNotIn("ACC-SINV", repr(out))


if __name__ == "__main__":
    unittest.main()
