"""Offline tests for mt940.py: the envelope and the light variant, the :61: and :86: reading, the references and the refusals.

Invented data only (the example IBAN of test_camt, made-up texts and references, round amounts), no network, no frappe:

    python3 -m unittest -v bi_finance.test_mt940        (from finance/apps/bi_finance)
"""

import unittest
from decimal import Decimal

from bi_finance import camt, mt940

IBAN = "CH56 0483 5012 3456 7800 9"
OPENING = ":60F:C261001CHF1000,00"
CLOSING = ":62F:C261031CHF1341,90"


def statement(lines, iban=IBAN, opening=OPENING, closing=CLOSING, newline="\r\n"):
    """A statement with the given lines between its opening and closing balance."""
    fields = [":20:INV-0001", f":25:{iban}", ":28C:1/1", opening, *lines, closing]
    return newline.join(fields) + newline


def envelope(text, newline="\r\n"):
    """The same statement in the SWIFT envelope: blocks 1 to 3, the text block 4, and the trailer."""
    return (
        "{1:F01UBSWCHZZAXXX0000000000}{2:I940UBSWCHZZAXXXN}{3:{108:INV-0001}}{4:"
        + newline
        + text
        + "-}{5:{MAC:00000000}}"
    )


# The fixture: opening 1000.00, the lines below, closing 1341.90 (round invented amounts).
LINES = [
    ":61:2610051005C100,00NTRFNONREF//BANK-A",
    ":86:Z45?Invented payer one",
    "Invented note on the payment",
    ":61:261006D8,10NTRFNONREF//BANK-B",
    ":86:Invented fee text",
    ":61:261008RC40,00NTRFNONREF//BANK-C",
    ":86:Z44?Invented reversal",
    ":61:261009RD40,00NTRFNONREF//BANK-D",
    ":86:Z59?Invented reversal in",
    ":61:2610101010D0,00NTRFNONREF//BANK-E",
    ":86:Balance closing of service prices",
    ":61:2610121012CR250,00NTRFCUSTREF1//BANK-F",
    "Invented supplementary line",
    ":86:Z45?Invented payer two",
]


class Envelope(unittest.TestCase):
    def test_envelope_and_light_variant_read_the_same(self):
        self.assertEqual(
            mt940.parse(envelope(statement(LINES))), mt940.parse(statement(LINES))
        )
        self.assertEqual(
            mt940.parse(envelope(statement(LINES))),
            mt940.parse(envelope(statement(LINES, newline="\n"))),
        )

    def test_crlf_and_lf_read_the_same(self):
        self.assertEqual(
            mt940.parse(statement(LINES)),
            mt940.parse(statement(LINES, newline="\n")),
        )

    def test_the_shape_is_camts(self):
        out = mt940.parse(statement(LINES))
        self.assertEqual(
            set(out),
            {"version", "kind", "iban", "currency", "balances", "transactions", "skipped"},
        )
        self.assertEqual((out["version"], out["kind"]), ("mt940", "mt940"))
        self.assertEqual(out["iban"], "CH5604835012345678009")
        self.assertEqual(out["currency"], "CHF")
        self.assertEqual(
            set(out["transactions"][0]),
            {
                "reference",
                "booking_date",
                "value_date",
                "currency",
                "deposit",
                "withdrawal",
                "end_to_end_id",
                "bank_party_name",
                "bank_party_iban",
                "reference_number",
                "description",
            },
        )

    def test_a_bare_text_block_without_envelope_reads(self):
        self.assertEqual(mt940.parse(statement(LINES))["iban"], "CH5604835012345678009")


class Lines(unittest.TestCase):
    def setUp(self):
        self.out = mt940.parse(envelope(statement(LINES)))
        self.tx = self.out["transactions"]

    def test_marks_give_money_in_or_out(self):
        marks = [(tx["deposit"], tx["withdrawal"]) for tx in self.tx]
        self.assertEqual(
            marks,
            [
                (Decimal("100.00"), Decimal("0")),  # C: in
                (Decimal("0"), Decimal("8.10")),  # D: out
                (Decimal("0"), Decimal("40.00")),  # RC: a reversal of a credit, out
                (Decimal("40.00"), Decimal("0")),  # RD: a reversal of a debit, in
                (Decimal("250.00"), Decimal("0")),  # C with a funds code letter
            ],
        )

    def test_the_text_of_the_codes_is_the_description_and_parties_are_none(self):
        self.assertEqual(
            self.tx[0]["description"], "Invented payer one Invented note on the payment"
        )
        for tx in self.tx:
            self.assertIsNone(tx["bank_party_name"])
            self.assertIsNone(tx["bank_party_iban"])
            self.assertIsNone(tx["reference_number"])

    def test_without_codes_the_whole_text_is_the_description(self):
        self.assertEqual(self.tx[1]["description"], "Invented fee text")

    def test_the_supplementary_line_is_the_description_when_there_is_no_text(self):
        lines = [
            ":61:261012C250,00NTRFNONREF//BANK-S",
            "Invented supplementary text",
            ":86:",
        ]
        out = mt940.parse(statement(lines, closing=":62F:C261012CHF1250,00"))
        self.assertEqual(out["transactions"][0]["description"], "Invented supplementary text")

    def test_the_description_is_cut_to_camts_length(self):
        long = "x" * (camt.DESCRIPTION_LENGTH + 20)
        lines = [":61:261012C1,00NTRFNONREF//BANK-L", f":86:{long}"]
        out = mt940.parse(statement(lines, closing=":62F:C261012CHF1001,00"))
        self.assertEqual(len(out["transactions"][0]["description"]), camt.DESCRIPTION_LENGTH)

    def test_the_booking_date_is_the_entry_date_else_the_value_date(self):
        self.assertEqual(self.tx[0]["value_date"], "2026-10-05")
        self.assertEqual(self.tx[0]["booking_date"], "2026-10-05")
        self.assertEqual(self.tx[1]["booking_date"], "2026-10-06")

    def test_an_entry_date_in_another_month_is_its_own_booking_date(self):
        lines = [":61:2610301101C1,00NTRFNONREF//BANK-M", ":86:"]
        out = mt940.parse(statement(lines, closing=":62F:C261030CHF1001,00"))
        self.assertEqual(out["transactions"][0]["value_date"], "2026-10-30")
        self.assertEqual(out["transactions"][0]["booking_date"], "2026-11-01")

    def test_an_entry_date_across_a_year_end_takes_the_right_year(self):
        lines = [
            ":61:261230" + "0101" + "C10,00NTRFNONREF//BANK-Y1",
            ":86:",
            ":61:270102" + "1229" + "D5,00NTRFNONREF//BANK-Y2",
            ":86:",
        ]
        out = mt940.parse(statement(lines, closing=":62F:C261230CHF1005,00"))
        self.assertEqual(
            [tx["booking_date"] for tx in out["transactions"]], ["2027-01-01", "2026-12-29"]
        )
        self.assertEqual(
            [tx["value_date"] for tx in out["transactions"]], ["2026-12-30", "2027-01-02"]
        )

    def test_the_customer_reference_is_the_end_to_end_id_unless_nonref(self):
        self.assertIsNone(self.tx[0]["end_to_end_id"])
        self.assertEqual(self.tx[4]["end_to_end_id"], "CUSTREF1")

    def test_a_line_without_a_bank_reference_takes_camts_hash(self):
        lines = [":61:2610121012C1,00NTRFNONREF//NONREF", ":86:"]
        out = mt940.parse(statement(lines, closing=":62F:C261012CHF1001,00"))
        self.assertTrue(out["transactions"][0]["reference"].startswith("camt-"))


class Balances(unittest.TestCase):
    def test_opening_plus_the_lines_is_the_closing(self):
        out = mt940.parse(envelope(statement(LINES)))
        total = sum(
            (tx["deposit"] - tx["withdrawal"] for tx in out["transactions"]), Decimal("0")
        )
        self.assertEqual(
            out["balances"]["OPBD"]["amount"] + total, out["balances"]["CLBD"]["amount"]
        )
        self.assertEqual(out["balances"]["OPBD"]["date"], "2026-10-01")
        self.assertEqual(out["balances"]["CLBD"]["date"], "2026-10-31")
        self.assertEqual(out["balances"]["CLBD"]["amount"], Decimal("1341.90"))

    def test_a_debit_balance_is_negative(self):
        out = mt940.parse(statement(LINES, opening=":60F:D261001CHF5,00"))
        self.assertEqual(out["balances"]["OPBD"]["amount"], Decimal("-5.00"))

    def test_the_closing_balance_may_be_missing(self):
        out = mt940.parse(statement(LINES, closing=""))
        self.assertNotIn("CLBD", out["balances"])


class References(unittest.TestCase):
    def test_the_reference_is_bank_reference_date_and_amount(self):
        out = mt940.parse(statement(LINES))
        self.assertEqual(out["transactions"][0]["reference"], "BANK-A-2026-10-05-100.00")

    def test_a_repeated_bank_reference_with_other_amounts_is_distinct(self):
        lines = [
            ":61:2610051005C100,00NTRFNONREF//SAME",
            ":86:",
            ":61:2610051005D100,50NTRFNONREF//SAME",
            ":86:",
        ]
        refs = [tx["reference"] for tx in mt940.parse(statement(lines, closing=":62F:C261005CHF999,50"))["transactions"]]
        self.assertEqual(len(set(refs)), 2)

    def test_two_identical_lines_get_a_running_number(self):
        lines = [
            ":61:2610051005C100,00NTRFNONREF//SAME",
            ":86:",
            ":61:2610051005C100,00NTRFNONREF//SAME",
            ":86:",
        ]
        refs = [tx["reference"] for tx in mt940.parse(statement(lines, closing=":62F:C261005CHF1200,00"))["transactions"]]
        self.assertEqual(refs, ["SAME-2026-10-05-100.00", "SAME-2026-10-05-100.00-2"])

    def test_the_same_line_in_an_overlapping_later_file_has_the_same_reference(self):
        first = [":61:2610051005C100,00NTRFNONREF//BANK-A", ":86:Invented one"]
        later = [
            ":61:2610061006D8,10NTRFNONREF//BANK-B",
            ":86:Invented two",
            ":61:2610051005C100,00NTRFNONREF//BANK-A",
            ":86:Invented one",
        ]
        ref_first = mt940.parse(statement(first))["transactions"][0]["reference"]
        ref_later = [
            tx["reference"]
            for tx in mt940.parse(statement(later, closing=":62F:C261006CHF1091,90"))["transactions"]
            if tx["booking_date"] == "2026-10-05"
        ]
        self.assertEqual(ref_later, [ref_first])


class Skipped(unittest.TestCase):
    def test_a_zero_line_is_skipped_and_counted(self):
        out = mt940.parse(statement(LINES))
        self.assertEqual(out["skipped"], {"not booked": 0, "covered": 0, "zero": 1})
        self.assertEqual(len(out["transactions"]), 5)
        self.assertNotIn("BANK-E", " ".join(tx["reference"] for tx in out["transactions"]))


class Refusals(unittest.TestCase):
    def test_a_file_that_is_no_mt940_is_refused(self):
        with self.assertRaises(camt.CamtError):
            mt940.parse(b"Invented text, not a statement")

    def test_an_xml_file_is_refused(self):
        with self.assertRaises(camt.CamtError):
            mt940.parse(b"<Document/>")

    def test_two_accounts_are_refused(self):
        text = statement(LINES).replace(":28C:", ":25:CH9300762011623852957\r\n:28C:", 1)
        with self.assertRaises(camt.CamtError):
            mt940.parse(text)

    def test_two_statements_are_refused(self):
        text = statement(LINES) + statement(LINES)
        with self.assertRaises(camt.CamtError):
            mt940.parse(text)

    def test_an_account_that_is_not_an_iban_is_refused(self):
        with self.assertRaises(camt.CamtError):
            mt940.parse(statement(LINES, iban="12345-67890"))

    def test_a_message_without_opening_balance_is_refused(self):
        with self.assertRaises(camt.CamtError):
            mt940.parse(statement(LINES, opening=""))

    def test_a_86_without_a_61_is_refused(self):
        with self.assertRaises(camt.CamtError):
            mt940.parse(statement([":86:Invented text"]))


class Encoding(unittest.TestCase):
    def test_latin1_bytes_are_read_when_they_are_not_utf8(self):
        content = statement([":61:2610051005C1,00NTRFNONREF//BANK-Q", ":86:Müller"]).encode("latin-1")
        self.assertEqual(mt940.parse(content)["transactions"][0]["description"], "Müller")

    def test_utf8_bytes_read_as_utf8(self):
        content = statement([":61:2610051005C1,00NTRFNONREF//BANK-Q", ":86:Müller"]).encode("utf-8")
        self.assertEqual(mt940.parse(content)["transactions"][0]["description"], "Müller")


if __name__ == "__main__":
    unittest.main()
