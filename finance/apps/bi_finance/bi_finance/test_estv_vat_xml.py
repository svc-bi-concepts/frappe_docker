"""Offline tests for the eCH-0217 2.0.0 file: the element order against ESTV's example, the codes and rates of the
form, and the amounts. Invented figures and an invented company only; the example in fixtures/ is ESTV's own sample
with dummy values. The module imports no frappe, so this runs on the host as part of the depot's light gate.
"""

import datetime
import os
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal

from bi_finance import estv_vat_xml

ECH_0217 = "{http://www.ech.ch/xmlns/eCH-0217/2}"
EXAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "eCH-0217_V2.0.0_example_effectiveReportingMethod.xml",
)
UTC = datetime.timezone.utc


def figures(**by_ziffer):
    # keyword form for readable tests: figures(z302=...) is figures({"302": ...})
    return {key[1:]: value for key, value in by_ziffer.items()}


def write(
    fig,
    start=datetime.date(2026, 7, 1),
    end=datetime.date(2026, 9, 30),
    company="Invented AG",
):
    xml = estv_vat_xml.declaration_xml(
        fig,
        uid="123456789",
        company=company,
        title="MWST 2026Q3",
        start_date=start,
        end_date=end,
        generated=datetime.datetime(2026, 10, 11, 8, 0, tzinfo=UTC),
        product_version="1.0",
    )
    return ET.fromstring(xml.encode("utf-8"))


def child_tags(el):
    return [child.tag.split("}")[1] for child in el]


def collapsed(tags):
    # one entry per run of the same tag
    return [tag for i, tag in enumerate(tags) if i == 0 or tags[i - 1] != tag]


def named(root, path):
    # find with the eCH-0217 prefix on every step
    steps = [ECH_0217 + step for step in path.split("/")]
    el = root
    for step in steps:
        el = el.find(step)
        if el is None:
            raise AssertionError(f"no element {step} on {path}")
    return el.text


class UidTest(unittest.TestCase):
    def test_nine_digits_of_the_uid(self):
        self.assertEqual(estv_vat_xml.uid_of("CHE-123.456.789 MWST"), "123456789")

    def test_other_digit_counts_are_refused(self):
        with self.assertRaises(ValueError):
            estv_vat_xml.uid_of("CHE-123.456 MWST")
        with self.assertRaises(ValueError):
            estv_vat_xml.uid_of(None)

    def test_only_the_grouped_che_number_is_read(self):
        # the grouping is part of the check: an ungrouped or prefixed number is refused, not read
        self.assertEqual(estv_vat_xml.uid_of("CHE-123.456.789"), "123456789")
        for other in (
            "CHE-123456789 MWST",
            "123.456.789",
            "CHE 123.456.789 MWST",
            "CHE-123.456.789 MWST 2",
        ):
            with self.assertRaises(ValueError, msg=other):
                estv_vat_xml.uid_of(other)


class StructureTest(unittest.TestCase):
    """The order and the names of the elements are ESTV's (its example file), so a wrong order fails here."""

    def setUp(self):
        self.example = ET.parse(EXAMPLE).getroot()
        self.root = write({})

    def test_top_level_order_as_the_example(self):
        self.assertEqual(child_tags(self.root), child_tags(self.example))

    def test_general_information_as_the_example(self):
        self.assertEqual(
            child_tags(self.root.find(ECH_0217 + "generalInformation")),
            child_tags(self.example.find(ECH_0217 + "generalInformation")),
        )

    def test_effective_reporting_method_as_the_example(self):
        # the example has one suppliesPerTaxRate; the form has one per rate, so the repeats are collapsed
        self.assertEqual(
            collapsed(
                child_tags(self.root.find(ECH_0217 + "effectiveReportingMethod"))
            ),
            collapsed(
                child_tags(self.example.find(ECH_0217 + "effectiveReportingMethod"))
            ),
        )

    def test_turnover_as_the_example_without_the_optional_variousDeduction(self):
        example = [
            tag
            for tag in child_tags(self.example.find(ECH_0217 + "turnoverComputation"))
            if tag != "variousDeduction"
        ]
        self.assertEqual(
            child_tags(self.root.find(ECH_0217 + "turnoverComputation")), example
        )

    def test_sending_application_uses_the_058_namespace(self):
        sending = self.root.find(
            ECH_0217 + "generalInformation/" + ECH_0217 + "sendingApplication"
        )
        self.assertEqual(
            child_tags(sending), ["manufacturer", "product", "productVersion"]
        )


class CodesTest(unittest.TestCase):
    def test_form_of_reporting_is_vereinnahmt(self):
        root = write({})
        self.assertEqual(named(root, "generalInformation/formOfReporting"), "2")

    def test_gross_or_net_and_submission(self):
        root = write({})
        self.assertEqual(named(root, "effectiveReportingMethod/grossOrNet"), "1")
        self.assertEqual(named(root, "generalInformation/typeOfSubmission"), "1")

    def test_uid_and_company_and_period(self):
        root = write(
            {},
            start=datetime.date(2026, 7, 1),
            end=datetime.date(2026, 9, 30),
            company="A & B AG",
        )
        self.assertEqual(named(root, "generalInformation/uid"), "CHE123456789")
        self.assertEqual(named(root, "generalInformation/organisationName"), "A & B AG")
        self.assertEqual(
            named(root, "generalInformation/reportingPeriodFrom"), "2026-07-01"
        )
        self.assertEqual(
            named(root, "generalInformation/reportingPeriodTill"), "2026-09-30"
        )
        self.assertEqual(
            named(root, "generalInformation/generationTime"), "2026-10-11T08:00:00Z"
        )

    def test_turnover_rows_have_the_rates_of_the_form(self):
        root = write({})
        rows = [
            (r.find(ECH_0217 + "taxRate").text, r.find(ECH_0217 + "turnover").text)
            for r in root.iter(ECH_0217 + "suppliesPerTaxRate")
        ]
        self.assertEqual(
            [rate for rate, _ in rows], ["7.7", "8.1", "2.5", "2.6", "3.7", "3.8"]
        )

    def test_sondersatz_before_2024_is_37_not_38(self):
        # erpnextswiss gave Ziffer 342 the rate 3.8 in every period; 3.7 is the rate it had until 2023
        root = write(figures(z342=Decimal("1000")))
        row = list(root.iter(ECH_0217 + "suppliesPerTaxRate"))[4]
        self.assertEqual(row.find(ECH_0217 + "taxRate").text, "3.7")
        self.assertEqual(row.find(ECH_0217 + "turnover").text, "1000.00")

    def test_acquisition_tax_before_2024_uses_the_old_rate_and_382(self):
        root = write(
            figures(z382=Decimal("500")),
            start=datetime.date(2023, 10, 1),
            end=datetime.date(2023, 12, 31),
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/acquisitionTax/taxRate"), "7.70"
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/acquisitionTax/turnover"), "500.00"
        )

    def test_acquisition_tax_from_2024_uses_the_new_rate_and_383(self):
        root = write(
            figures(z383=Decimal("600")),
            start=datetime.date(2024, 1, 1),
            end=datetime.date(2024, 3, 31),
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/acquisitionTax/taxRate"), "8.10"
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/acquisitionTax/turnover"), "600.00"
        )


class AmountsTest(unittest.TestCase):
    def test_two_decimals_and_missing_as_zero(self):
        root = write(figures(z200=Decimal("1234.5"), z500=Decimal("-12.3")))
        self.assertEqual(
            named(root, "turnoverComputation/totalConsideration"), "1234.50"
        )
        self.assertEqual(named(root, "payableTax"), "-12.30")
        self.assertEqual(named(root, "turnoverComputation/suppliesAbroad"), "0.00")

    def test_rounds_half_up_to_the_rappen(self):
        root = write(figures(z302=Decimal("10.005")))
        row = list(root.iter(ECH_0217 + "suppliesPerTaxRate"))[0]
        self.assertEqual(row.find(ECH_0217 + "turnover").text, "10.01")

    def test_input_tax_rows(self):
        root = write(figures(z400=Decimal("70"), z405=Decimal("8"), z415=Decimal("-2")))
        self.assertEqual(
            named(root, "effectiveReportingMethod/inputTaxMaterialAndServices"), "70.00"
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/inputTaxInvestments"), "8.00"
        )
        self.assertEqual(
            named(root, "effectiveReportingMethod/inputTaxCorrections"), "-2.00"
        )

    def test_utf8_declaration_line_and_umlauts(self):
        xml = estv_vat_xml.declaration_xml(
            {},
            uid="123456789",
            company="Müller AG",
            title="MWST",
            start_date=datetime.date(2026, 7, 1),
            end_date=datetime.date(2026, 9, 30),
            generated=datetime.datetime(2026, 10, 11, tzinfo=UTC),
            product_version="1.0",
        )
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
        self.assertIn("Müller AG", xml)


if __name__ == "__main__":
    unittest.main()
