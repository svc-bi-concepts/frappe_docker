"""Offline tests for the payment basis's fields (mwst_payment.declaration_fields). Invented documents and figures only.

The module imports no frappe, so this runs on the host as part of the depot's light gate.
"""

import datetime
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal

from bi_finance import estv_vat_xml
from bi_finance import mwst_payment as mp

D = Decimal

# invented template names with bexio's ids, as templates_by_name() gives them
TEMPLATE_IDS = {
    "UN81 8.1% Normalsatz": "28",
    "VM77 7.7% Normalsatz Material/DL": "22",
    "BZB81 8.1% Bezugsteuer Invest./Aufwand": "32",
    "VES 8.1% Vorsteuerkorrektur nachträglich": "40",
    "UR26 2.6% Reduzierter Satz": "29",
    "UEX 0% Export, steuerbefreit": "3",
    "ULA 0% Ausland": "4",
    "MEL 0% Meldeverfahren": "5",
    "U00 0% Null-Satz": "48",
}
QUARTER = (datetime.date(2026, 7, 1), datetime.date(2026, 9, 30))


def row(name, tax, add_deduct="Add"):
    return {
        "description": name,
        "base_tax_amount": D(str(tax)),
        "tax_amount": D(str(tax)),
        "add_deduct_tax": add_deduct,
    }


def sale(posting, *rows, net="0"):
    return {"posting_date": posting, "base_net_total": D(net), "taxes": list(rows)}


class PeriodTest(unittest.TestCase):
    def test_quarter_and_half_year_bounds(self):
        self.assertEqual(mp.period_bounds("2026Q3"), QUARTER)
        self.assertEqual(
            mp.period_bounds("2026H2"),
            (datetime.date(2026, 7, 1), datetime.date(2026, 12, 31)),
        )

    def test_other_periods_are_refused(self):
        with self.assertRaises(ValueError):
            mp.period_bounds("2026Q5")


class FieldsTest(unittest.TestCase):
    def fields(self, sales=(), purchases=()):
        totals = mp.plan(list(sales), list(purchases), TEMPLATE_IDS)
        mp.journal_totals([], totals)
        return mp.declaration_fields(totals)

    def test_empty_period_is_all_zero_and_every_field_is_set(self):
        fields = self.fields()
        self.assertEqual(fields["total_revenue"], D("0"))
        self.assertEqual(fields["payable_tax"], D("0"))
        for name in (
            "missing_pretax",
            "pretax_correction_mixed",
            "pretax_correction_other",
            "tax_free_services",
            "revenue_abroad",
            "transfers",
            "non_taxable_services",
            "losses",
        ):
            self.assertEqual(fields[name], D("0"), name)

    def test_a_quarter_with_sales_purchases_and_bezugsteuer(self):
        # invented: 1000.00 of sales at 8.1 % (2024 row), 1000.00 of material bought at 7.7 %, and a Bezugsteuer of
        # 1000.00 at 8.1 % (Add on 405, Deduct on 383, the Deduct being the owed tax of a document from 2024)
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "81.00"),
                    net="1000.00",
                )
            ],
            purchases=[
                {
                    "posting_date": datetime.date(2026, 8, 5),
                    "base_net_total": D("0"),
                    "taxes": [row("VM77 7.7% Normalsatz Material/DL", "77.00")],
                },
                {
                    "posting_date": datetime.date(2026, 8, 6),
                    "base_net_total": D("0"),
                    "taxes": [
                        row("BZB81 8.1% Bezugsteuer Invest./Aufwand", "81.00"),
                        row(
                            "BZB81 8.1% Bezugsteuer Invest./Aufwand",
                            "81.00",
                            add_deduct="Deduct",
                        ),
                    ],
                },
            ],
        )
        self.assertEqual(fields["normal_amount_2024"], D("1000.00"))
        self.assertEqual(fields["normal_rate_2024"], D("8.1"))
        self.assertEqual(fields["normal_tax_2024"], D("81.00"))
        self.assertEqual(fields["normal_amount"], D("0"))
        self.assertEqual(fields["pretax_material"], D("77.00"))
        self.assertEqual(fields["pretax_investments"], D("81.00"))
        self.assertEqual(fields["additional_amount_2024"], D("1000.00"))
        self.assertEqual(fields["additional_tax_2024"], D("81.00"))
        # 399 is 81 (sales) plus 81 (Bezugsteuer owed); 479 is 77 plus 81; 500 is what is left
        self.assertEqual(fields["total_tax"], D("162.00"))
        self.assertEqual(fields["total_pretax_reductions"], D("158.00"))
        self.assertEqual(fields["payable_tax"], D("4.00"))

    def test_a_rate_row_is_its_base_times_its_rate_as_the_form_has_it(self):
        # invented: a tax of 27.00 at 8.1 % is a base of 333.33 (derived), and the form's tax is that base at 8.1 %
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "27.00"),
                    net="333.33",
                )
            ]
        )
        self.assertEqual(fields["normal_amount_2024"], D("333.33"))
        self.assertEqual(fields["normal_tax_2024"], D("27.00"))

    def test_the_two_point_six_row_takes_its_own_rate(self):
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UR26 2.6% Reduzierter Satz", "26.00"),
                    net="1000.00",
                )
            ]
        )
        self.assertEqual(fields["reduced_amount_2024"], D("1000.00"))
        self.assertEqual(fields["reduced_rate_2024"], D("2.6"))
        self.assertEqual(fields["reduced_tax_2024"], D("26.00"))

    def test_an_unmapped_tax_row_is_refused_not_dropped(self):
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(datetime.date(2026, 8, 3), row("XX99 9.9% unknown", "9.90"))
                ]
            )
        self.assertIn("no known template", str(caught.exception))

    def test_a_tax_on_415_is_refused_as_the_form_has_no_decided_figure(self):
        with self.assertRaises(ValueError) as caught:
            self.fields(
                purchases=[
                    {
                        "posting_date": datetime.date(2026, 8, 5),
                        "base_net_total": D("0"),
                        "taxes": [
                            row("VES 8.1% Vorsteuerkorrektur nachträglich", "8.10")
                        ],
                    }
                ]
            )
        self.assertIn("415", str(caught.exception))


class DeductionsTest(unittest.TestCase):
    """The Umsatz deductions 220 to 235: the zero-rate sales rows of the payment basis, and the plausibility check."""

    def fields(self, sales=()):
        totals = mp.plan(list(sales), [], TEMPLATE_IDS)
        mp.journal_totals([], totals)
        return mp.declaration_fields(totals)

    def test_a_quarter_with_only_rate_rows_leaves_the_deductions_at_zero(self):
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "81.00"),
                    net="1000.00",
                )
            ]
        )
        self.assertEqual(fields["total_revenue"], D("1000.00"))
        for name in (
            "tax_free_services",
            "revenue_abroad",
            "transfers",
            "non_taxable_services",
            "losses",
        ):
            self.assertEqual(fields[name], D("0"), name)
        self.assertEqual(fields["normal_amount_2024"], D("1000.00"))

    def test_export_revenue_fills_220_and_299_stays_consistent(self):
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UEX 0% Export, steuerbefreit", "0"),
                    net="1000.00",
                )
            ]
        )
        self.assertEqual(fields["total_revenue"], D("1000.00"))
        self.assertEqual(fields["tax_free_services"], D("1000.00"))
        self.assertEqual(fields["normal_amount_2024"], D("0"))
        self.assertEqual(fields["payable_tax"], D("0"))

    def test_a_mixed_document_puts_its_zero_rate_net_whole_into_220(self):
        # the per-row net is not in the data: the document's net less the derived base of its rated row
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "81.00"),
                    row("UEX 0% Export, steuerbefreit", "0"),
                    net="1081.00",
                )
            ]
        )
        self.assertEqual(fields["normal_amount_2024"], D("1000.00"))
        self.assertEqual(fields["tax_free_services"], D("81.00"))
        self.assertEqual(fields["total_revenue"], D("1081.00"))

    def test_abroad_revenue_fills_221_and_the_meldeverfahren_225(self):
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3), row("ULA 0% Ausland", "0"), net="700.00"
                ),
                sale(
                    datetime.date(2026, 8, 4),
                    row("MEL 0% Meldeverfahren", "0"),
                    net="200.00",
                ),
            ]
        )
        self.assertEqual(fields["revenue_abroad"], D("700.00"))
        self.assertEqual(fields["transfers"], D("200.00"))
        self.assertEqual(fields["tax_free_services"], D("0"))

    def test_a_code_with_no_deduction_row_refuses_and_is_named(self):
        # UNO, SUB, SPE and U00 have no row on the form; their net is never guessed into one
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(
                        datetime.date(2026, 8, 3),
                        row("U00 0% Null-Satz", "0"),
                        net="50.00",
                    )
                ]
            )
        message = str(caught.exception)
        self.assertIn("U00", message)
        self.assertIn("50.00", message)

    def test_a_net_that_would_split_between_rows_refuses_naming_the_codes(self):
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(
                        datetime.date(2026, 8, 3),
                        row("UEX 0% Export, steuerbefreit", "0"),
                        row("ULA 0% Ausland", "0"),
                        net="30.00",
                    )
                ]
            )
        message = str(caught.exception)
        self.assertIn("UEX", message)
        self.assertIn("ULA", message)

    def test_the_derived_bases_drift_within_the_rounding_of_their_documents(self):
        # a tax of 0.08 at 8.1 % derives a base of 0.99, so each document keeps a rappen of 200 out of the rate row;
        # forty such documents drift by 0.40, over a flat 0.05 but within the half-rappen bound of their taxes
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "0.08"),
                    net="1.00",
                )
                for _ in range(40)
            ]
        )
        self.assertEqual(fields["total_revenue"], D("40.00"))
        self.assertEqual(fields["normal_amount_2024"], D("39.60"))

    def test_a_difference_beyond_the_rounding_refuses_with_the_difference(self):
        # invented: a document whose net is 5.00 more than its taxed rows and its zero-rate code carries
        # (a rated row beside no zero-rate row: nothing takes the 5.00), so 200 is off the rate base by far more
        # than the half-rappen bound of one 8.1 % tax
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(
                        datetime.date(2026, 8, 3),
                        row("UN81 8.1% Normalsatz", "81.00"),
                        net="1005.00",
                    )
                ]
            )
        self.assertIn("5.00", str(caught.exception))

    def test_a_code_with_no_row_refuses_even_when_its_net_is_within_the_rounding(self):
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(
                        datetime.date(2026, 8, 3),
                        row("UN81 8.1% Normalsatz", "81.00"),
                        row("U00 0% Null-Satz", "0"),
                        net="1000.03",
                    )
                ]
            )
        self.assertIn("U00", str(caught.exception))


def jline(voucher, account, amount, key="manual-1", day=datetime.date(2026, 8, 10)):
    """A GL row of a Journal Entry as fetch_journal_lines() gives it: the net of debit less credit, keyed by bexio_id."""
    return {
        "voucher": voucher,
        "posting_date": day,
        "account": account,
        "amount": D(amount),
        "key": key,
    }


# invented manual entry with sales tax: 1000.00 of revenue at 8.1 %, 81.00 of tax booked by hand on 2200
MANUAL_SALE = [
    jline("J1", "3200", "-1000.00"),
    jline("J1", "2200", "-81.00"),
    jline("J1", "1020", "1081.00"),
]


class JournalBasesTest(unittest.TestCase):
    """A manual Journal Entry with sales tax: its base is Entgelt (Ziffer 200) as well as the rate row's base."""

    def fields(self, sales=(), lines=()):
        totals = mp.plan(list(sales), [], TEMPLATE_IDS)
        mp.journal_totals(list(lines), totals)
        return mp.declaration_fields(totals)

    def test_a_journal_only_quarter_fills_with_200_equal_to_its_base(self):
        fields = self.fields(lines=MANUAL_SALE)
        self.assertEqual(fields["total_revenue"], D("1000.00"))
        self.assertEqual(fields["normal_amount_2024"], D("1000.00"))
        self.assertEqual(fields["normal_tax_2024"], D("81.00"))
        self.assertEqual(fields["payable_tax"], D("81.00"))

    def test_a_mixed_quarter_adds_the_journal_base_to_the_sales_base(self):
        fields = self.fields(
            sales=[
                sale(
                    datetime.date(2026, 8, 3),
                    row("UN81 8.1% Normalsatz", "81.00"),
                    net="1000.00",
                )
            ],
            lines=MANUAL_SALE,
        )
        self.assertEqual(fields["total_revenue"], D("2000.00"))
        self.assertEqual(fields["normal_amount_2024"], D("2000.00"))
        self.assertEqual(fields["normal_tax_2024"], D("162.00"))

    def test_a_difference_names_the_rate_bases_that_make_it(self):
        # invented: a sales document 5.00 over its taxed row, beside the manual entry: the refusal names both sources
        with self.assertRaises(ValueError) as caught:
            self.fields(
                sales=[
                    sale(
                        datetime.date(2026, 8, 3),
                        row("UN81 8.1% Normalsatz", "81.00"),
                        net="1005.00",
                    )
                ],
                lines=MANUAL_SALE,
            )
        message = str(caught.exception)
        self.assertIn("5.00", message)
        self.assertIn("UN81 1000.00", message)
        self.assertIn("journal entries 1000.00", message)


# invented manual entry with Bezugsteuer: 38.38 of tax owed on 2203 (credit) and input tax on 1170, dated in 2026
MANUAL_BEZUG = [jline("B1", "1170", "38.38"), jline("B1", "2203", "-38.38")]


class JournalBezugsteuerTest(unittest.TestCase):
    """A manual Journal Entry's Bezugsteuer owed: its base is derived at the period's rate, as a purchase's is, and it is
    not Entgelt (Ziffer 200 does not take it)."""

    def fields(self, lines=(), kept=None):
        totals = mp.plan([], [], TEMPLATE_IDS)
        mp.journal_totals(list(lines), totals)
        return mp.declaration_fields(totals, kept), totals

    def test_the_owed_tax_gets_its_base_at_the_rate_of_2026(self):
        fields, totals = self.fields(lines=MANUAL_BEZUG)
        self.assertEqual(fields["additional_tax_2024"], D("38.38"))
        self.assertEqual(fields["additional_amount_2024"], D("473.83"))
        self.assertEqual(fields["total_revenue"], D("0"))
        self.assertEqual(fields["total_tax"], D("38.38"))
        self.assertEqual(fields["pretax_material"], D("38.38"))
        self.assertEqual(fields["payable_tax"], D("0"))

    def test_base_times_rate_is_the_tax_to_the_rappen(self):
        # invented: 41.16 of tax at 8.1 % is a base of 508.15, and 508.15 at 8.1 % is 41.16 again
        fields, _ = self.fields(
            lines=[jline("B2", "1170", "41.16"), jline("B2", "2203", "-41.16")]
        )
        self.assertEqual(fields["additional_amount_2024"], D("508.15"))
        self.assertEqual(fields["additional_tax_2024"], D("41.16"))
        self.assertEqual(
            (fields["additional_amount_2024"] * D("8.1") / 100).quantize(D("0.01")),
            D("41.16"),
        )

    def test_before_2024_the_base_is_at_the_old_rate_and_in_382(self):
        # invented: 31.99 of tax owed in 2023 at 7.7 % is a base of 415.45 in Ziffer 382
        lines = [
            jline("B3", "1170", "31.99", day=datetime.date(2023, 11, 10)),
            jline("B3", "2203", "-31.99", day=datetime.date(2023, 11, 10)),
        ]
        fields, totals = self.fields(lines=lines)
        self.assertEqual(totals.base_of("382"), D("415.45"))
        self.assertEqual(totals.tax_of("382"), D("31.99"))
        self.assertEqual(fields["additional_amount"], D("415.45"))
        self.assertEqual(fields["additional_tax"], D("31.99"))
        self.assertEqual(fields["total_revenue"], D("0"))

    def test_the_xml_declares_the_whole_bezugsteuer_as_its_base(self):
        # the ESTV file carries the base only, at 8.10: its turnover is the base the tax came from
        fields, _ = self.fields(lines=MANUAL_BEZUG)
        # as create_transfer_file reads them: a field the fill does not set is zero
        figures = {
            ziffer: fields.get(name, D("0"))
            for ziffer, name in mp.FIELD_BY_ZIFFER.items()
        }
        xml = estv_vat_xml.declaration_xml(
            figures,
            uid="123456789",
            company="Invented AG",
            title="MWST 2026Q3",
            start_date=datetime.date(2026, 7, 1),
            end_date=datetime.date(2026, 9, 30),
            generated=datetime.datetime(
                2026, 10, 12, 8, 0, tzinfo=datetime.timezone.utc
            ),
            product_version="1.0",
        )
        root = ET.fromstring(xml.encode("utf-8"))
        acquisition = root.find(".//{http://www.ech.ch/xmlns/eCH-0217/2}acquisitionTax")
        self.assertEqual(
            acquisition.find("{http://www.ech.ch/xmlns/eCH-0217/2}taxRate").text, "8.10"
        )
        self.assertEqual(
            acquisition.find("{http://www.ech.ch/xmlns/eCH-0217/2}turnover").text,
            "473.83",
        )


class FormSumsTest(unittest.TestCase):
    """289 and 299 as erpnextswiss's form sets them on save: 289 is the deductions and the draft's 280, 299 is 200 less
    the draft's 205 less 289. Invented figures: 510.49 of 8.1 % sales and 412.37 of export.
    """

    def fields(self, kept=None):
        sales = [
            sale(
                datetime.date(2026, 8, 3),
                row("UN81 8.1% Normalsatz", "41.35"),
                net="510.49",
            ),
            sale(
                datetime.date(2026, 8, 4),
                row("UEX 0% Export, steuerbefreit", "0"),
                net="412.37",
            ),
        ]
        totals = mp.plan(sales, [], TEMPLATE_IDS)
        mp.journal_totals([], totals)
        return mp.declaration_fields(totals, kept)

    def test_deductions_and_taxable_revenue_without_a_draft_of_280_and_205(self):
        fields = self.fields()
        self.assertEqual(fields["total_revenue"], D("922.86"))
        self.assertEqual(fields["total_deductions"], D("412.37"))
        self.assertEqual(fields["taxable_revenue"], D("510.49"))

    def test_the_draft_keeps_its_280_in_289_and_its_205_out_of_299(self):
        # invented: 44.07 of 280 and 43.33 of 205 held on the draft
        fields = self.fields({"misc": D("44.07"), "non_taxable_revenue": D("43.33")})
        self.assertEqual(fields["total_deductions"], D("456.44"))
        self.assertEqual(fields["taxable_revenue"], D("423.09"))
        self.assertEqual(fields["total_revenue"], D("922.86"))

    def test_an_empty_period_has_zero_in_289_and_299(self):
        totals = mp.plan([], [], TEMPLATE_IDS)
        mp.journal_totals([], totals)
        fields = mp.declaration_fields(totals)
        self.assertEqual(fields["total_deductions"], D("0"))
        self.assertEqual(fields["taxable_revenue"], D("0"))


if __name__ == "__main__":
    unittest.main()
