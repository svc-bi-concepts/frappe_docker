"""Offline tests of the Salary Component accounts: the fixture carries the account rows that swiss_rates names, and nothing
else. Invented names, no site:

    python3 -m unittest bi_payroll.test_payroll_accounts

from this app's directory. The chart check (every number is an account of the KMU chart) needs erpnextswiss's
accounts_template.csv: set BI_COA_CSV to its path and it runs, otherwise it is skipped.
"""

import csv
import json
import os
import unittest

from bi_payroll.swiss_rates import (
    COMPONENT_ACCOUNTS,
    EMPLOYER_CONTRIBUTION_ACCOUNTS,
    PAYROLL_PAYABLE_ACCOUNT,
    STRUCTURE_EMPLOYER_CONTRIBUTIONS,
)

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANY = "BI Concepts"


def components():
    with open(
        os.path.join(HERE, "fixtures", "salary_component.json"), encoding="utf-8"
    ) as f:
        return {c["name"]: c for c in json.load(f)}


class ComponentAccounts(unittest.TestCase):
    def test_each_mapped_component_has_one_account_row_for_the_company(self):
        fixture = components()
        for name, number in COMPONENT_ACCOUNTS.items():
            with self.subTest(component=name):
                rows = fixture[name]["accounts"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["company"], COMPANY)
                self.assertTrue(
                    rows[0]["account"].startswith(f"{number} - "), rows[0]["account"]
                )
                self.assertTrue(
                    rows[0]["account"].endswith(" - bic"), rows[0]["account"]
                )
                self.assertFalse(rows[0].get("liability_account"), name)

    def test_each_employer_share_is_an_employer_contribution_with_an_expense_and_a_liability(
        self,
    ):
        fixture = components()
        for name, (expense, liability) in EMPLOYER_CONTRIBUTION_ACCOUNTS.items():
            with self.subTest(component=name):
                self.assertEqual(fixture[name]["type"], "Employer Contribution")
                rows = fixture[name]["accounts"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["company"], COMPANY)
                self.assertTrue(
                    rows[0]["account"].startswith(f"{expense} - "), rows[0]["account"]
                )
                self.assertTrue(
                    rows[0]["liability_account"].startswith(f"{liability} - "),
                    rows[0]["liability_account"],
                )
                self.assertTrue(
                    rows[0]["account"].endswith(" - bic"), rows[0]["account"]
                )
                self.assertTrue(
                    rows[0]["liability_account"].endswith(" - bic"),
                    rows[0]["liability_account"],
                )

    def test_the_employer_shares_are_the_structure_ones(self):
        self.assertEqual(
            sorted(EMPLOYER_CONTRIBUTION_ACCOUNTS),
            sorted(STRUCTURE_EMPLOYER_CONTRIBUTIONS),
        )

    def test_every_non_statistical_component_is_mapped_once(self):
        # a component with no account would reach the accrual with none and stop it; one in both maps would be booked twice
        fixture = components()
        mapped = set(COMPONENT_ACCOUNTS) | set(EMPLOYER_CONTRIBUTION_ACCOUNTS)
        self.assertEqual(
            set(COMPONENT_ACCOUNTS) & set(EMPLOYER_CONTRIBUTION_ACCOUNTS), set()
        )
        self.assertEqual(
            sorted(mapped),
            sorted(
                n
                for n, c in fixture.items()
                if not c["statistical_component"]
                and n
                not in {
                    "Hourly Wage",
                    "Vacation Compensation",
                    "Expenses Effective",
                    "Expenses Lump Sum",
                    "Rent Deduction",
                    "Advance Payment",
                }
            ),
        )

    def test_statistical_bvg_age_stays_without_an_account(self):
        self.assertEqual(components()["BVG Age"]["accounts"], [])

    def test_the_payable_is_a_number_of_the_mapping_not_an_employee_account(self):
        self.assertEqual(PAYROLL_PAYABLE_ACCOUNT, "1091")
        self.assertNotIn(PAYROLL_PAYABLE_ACCOUNT, COMPONENT_ACCOUNTS.values())


# The accounts bexio's chart adds to the KMU template (private/bexio-mapping.md, the rows "add under"): erpnextswiss's
# accounts_template.csv has none of them. They exist on the live site; on a fresh one the 'accounts' step of
# finance/scripts/swiss-setup.py creates them, from a table test_swiss_setup.py compares with this list.
BEXIO_ADDED = {"2270", "2271", "2273", "2274", "2279", "5003", "5720"}


@unittest.skipUnless(
    os.environ.get("BI_COA_CSV"), "BI_COA_CSV: erpnextswiss's accounts_template.csv"
)
class ChartTemplate(unittest.TestCase):
    def test_every_number_is_an_account_of_the_kmu_chart_or_bexio_added(self):
        with open(os.environ["BI_COA_CSV"], encoding="utf-8") as f:
            numbers = {
                row["account_number"].strip() for row in csv.DictReader(f)
            } | BEXIO_ADDED
        expense = {expense for expense, _ in EMPLOYER_CONTRIBUTION_ACCOUNTS.values()}
        liability = {
            liability for _, liability in EMPLOYER_CONTRIBUTION_ACCOUNTS.values()
        }
        for number in (
            set(COMPONENT_ACCOUNTS.values())
            | expense
            | liability
            | {PAYROLL_PAYABLE_ACCOUNT}
        ):
            with self.subTest(number=number):
                self.assertIn(number, numbers)


if __name__ == "__main__":
    unittest.main()
