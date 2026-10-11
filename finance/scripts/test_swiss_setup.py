"""Offline tests for the pure parts of swiss-setup.py: no site, no network.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

# swiss-setup.py imports frappe, which only exists in the backend container.
# The pure helpers under test never touch it.
sys.modules.setdefault("frappe", mock.MagicMock())
sys.modules.setdefault("frappe.utils", mock.MagicMock())

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swiss-setup.py")
_spec = importlib.util.spec_from_file_location("swiss_setup", _path)
swiss_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swiss_setup)


class DeleteRevokesTest(unittest.TestCase):
    def test_revokes_every_role_but_the_kept_one(self):
        perms = [
            ("Accounts Manager", 0, 1),
            ("Accounts User", 0, 1),
            ("System Manager", 0, 1),
        ]
        self.assertEqual(
            swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]),
            [("Accounts User", 0), ("System Manager", 0)],
        )

    def test_roles_without_delete_are_left_alone(self):
        perms = [("Accounts User", 0, 0), ("Accounts Manager", 0, 1)]
        self.assertEqual(swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]), [])

    def test_empty_when_nothing_may_delete(self):
        # A doctype where no role but the kept one can delete: nothing to revoke.
        self.assertEqual(swiss_setup.delete_revokes([("Sales User", 0, 0)], keep=["Accounts Manager"]), [])

    def test_revokes_on_every_permlevel(self):
        # The kept role is matched by name, whatever the permlevel.
        perms = [("Accounts User", 1, 1)]
        self.assertEqual(swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]), [("Accounts User", 1)])


class ParseFreezeTest(unittest.TestCase):
    def test_date_alone_is_a_dry_run(self):
        self.assertEqual(swiss_setup.parse_freeze(["2025-12-31"]), ("2025-12-31", False))

    def test_apply_flag_after_the_date(self):
        self.assertEqual(swiss_setup.parse_freeze(["2025-12-31", "--apply"]), ("2025-12-31", True))

    def test_apply_flag_before_the_date(self):
        self.assertEqual(swiss_setup.parse_freeze(["--apply", "2025-12-31"]), ("2025-12-31", True))

    def test_rejects_a_missing_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze([])

    def test_rejects_only_the_flag(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["--apply"])

    def test_rejects_two_dates(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-12-31", "2026-12-31"])

    def test_rejects_a_repeated_flag(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-12-31", "--apply", "--apply"])

    def test_rejects_something_that_is_not_a_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["31.12.2025"])

    def test_rejects_an_impossible_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-02-30"])


class BexioDoctypesTest(unittest.TestCase):
    def test_employee_is_keyed_by_bexio_id_for_the_payroll_import(self):
        self.assertIn(("Employee", "employee_name"), swiss_setup.BEXIO_DOCTYPES)

    def test_no_doctype_is_listed_twice(self):
        doctypes = [dt for dt, _after in swiss_setup.BEXIO_DOCTYPES]
        self.assertEqual(len(doctypes), len(set(doctypes)))


class StepsTest(unittest.TestCase):
    def test_gebuev_is_a_step_and_freeze_is_not(self):
        self.assertIn("gebuev", swiss_setup.STEPS)
        self.assertNotIn("freeze", swiss_setup.STEPS)

    def test_payslip_is_a_step(self):
        self.assertIn("payslip", swiss_setup.STEPS)

    def test_the_payslip_html_is_the_bi_payroll_app_s_template(self):
        self.assertIn("apps/bi_payroll/bi_payroll/lohnabrechnung.html", swiss_setup.PAYSLIP_HTML)

    def test_hr_is_a_step(self):
        self.assertIn("hr", swiss_setup.STEPS)

    def test_payroll_settings_want_holds_benchis_decisions(self):
        # Benchi 2026-10-11: leave based, holidays counted, no slip e-mails
        # (Salary Slip.on_submit mails the slip when the check is on).
        self.assertEqual(swiss_setup.PAYROLL_SETTINGS_WANT, {
            "payroll_based_on": "Leave",
            "include_holidays_in_total_working_days": 1,
            "email_salary_slip_to_employee": 0,
        })

    def test_hr_settings_wants_nothing(self):
        # HRMS v16.50.0 has no switch for self-service or onboarding e-mails;
        # the step must not invent one.
        self.assertEqual(swiss_setup.HR_SETTINGS_WANT, {})

    def test_the_wanted_roles_are_the_goal_four(self):
        self.assertEqual(swiss_setup.HR_ROLES,
                         ["HR Manager", "HR User", "Payroll Manager", "Leave Approver"])


class PayslipChangesTest(unittest.TestCase):
    # The Lohnabrechnung format is in place when it carries the file's HTML and is not disabled.
    IN_PLACE = {"html": "<p>slip</p>", "disabled": 0}

    def test_a_format_in_place_wants_no_change(self):
        self.assertEqual(swiss_setup.payslip_changes(self.IN_PLACE, "<p>slip</p>"), {})

    def test_changed_html_updates_the_format(self):
        self.assertEqual(swiss_setup.payslip_changes(self.IN_PLACE, "<p>new</p>"), {"html": "<p>new</p>"})

    def test_a_disabled_format_is_enabled_again(self):
        self.assertEqual(swiss_setup.payslip_changes({**self.IN_PLACE, "disabled": 1}, "<p>slip</p>"), {"disabled": 0})


class HrGrantsTest(unittest.TestCase):
    WANTED = swiss_setup.HR_ROLES

    def test_adds_the_missing_roles_the_site_offers(self):
        site = ["HR Manager", "HR User", "Leave Approver"]
        to_add, not_offered = swiss_setup.hr_grants(self.WANTED, site, ["HR User"])
        self.assertEqual(to_add, ["HR Manager", "Leave Approver"])
        self.assertEqual(not_offered, ["Payroll Manager"])

    def test_a_role_without_a_role_document_is_never_added(self):
        # HRMS v16.50.0 ships no Payroll Manager: noted, not created.
        to_add, not_offered = swiss_setup.hr_grants(self.WANTED, ["HR Manager", "HR User", "Leave Approver"], [])
        self.assertEqual(to_add, ["HR Manager", "HR User", "Leave Approver"])
        self.assertEqual(not_offered, ["Payroll Manager"])

    def test_nothing_to_do_when_all_in_place(self):
        self.assertEqual(swiss_setup.hr_grants(self.WANTED, self.WANTED, self.WANTED), ([], []))


class SingleFixesTest(unittest.TestCase):
    def test_sets_only_offered_fields_that_differ(self):
        want = {"payroll_based_on": "Leave", "include_holidays_in_total_working_days": 1}
        current = {"payroll_based_on": "Leave", "include_holidays_in_total_working_days": 0}
        changes, not_offered = swiss_setup.single_fixes(want, current, set(want))
        self.assertEqual(changes, [("include_holidays_in_total_working_days", 0, 1)])
        self.assertEqual(not_offered, [])

    def test_an_already_right_value_changes_nothing(self):
        self.assertEqual(swiss_setup.single_fixes({"a": 0}, {"a": 0}, {"a"}), ([], []))

    def test_a_field_the_single_has_not_is_reported_not_set(self):
        changes, not_offered = swiss_setup.single_fixes({"a": 1, "b": 2}, {"a": 1, "b": None}, {"a"})
        self.assertEqual(changes, [])
        self.assertEqual(not_offered, ["b"])


# Invented IBANs and account names only: the test data is not company data.
TEST_IBAN = "CH0000000000000000001"
TEST_IBAN_WISE = "CH0000000000000000002"
UBS_BIC = "UBSWCHZH80A"


class PaymentFixesTest(unittest.TestCase):
    def test_copies_iban_to_gl_account_and_sets_public_bic(self):
        # The live case: Bank Account with an IBAN, empty GL Account and an empty Bank.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        fixes = swiss_setup.payment_fixes(bank_accounts, {"1020 UBS": {"iban": None, "bic": None}}, {"UBS Switzerland AG": None})
        self.assertEqual(fixes, [
            ("Account", "1020 UBS", "iban", TEST_IBAN),
            ("Bank", "UBS Switzerland AG", "swift_number", UBS_BIC),
            ("Account", "1020 UBS", "bic", UBS_BIC),
        ])

    def test_second_run_changes_nothing(self):
        # Once the fixes are applied, the next run finds every field set.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": UBS_BIC}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}), [])

    def test_a_value_entered_by_hand_is_not_overwritten(self):
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": "CH0000000000000000009", "bic": "OTHERBIC"}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": "OTHERBIC"}), [])

    def test_bank_swift_number_kept_and_copied_to_gl_bic(self):
        # A Bank that already has a swift number keeps it; the GL Account takes it.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": None}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}),
                         [("Account", "1020 UBS", "bic", UBS_BIC)])

    def test_bank_without_public_bic_gets_only_the_iban(self):
        # Wise has no BIC in the table: its IBAN is copied, nothing else is set.
        bank_accounts = [{"name": "Wise", "account": "1030 Wise", "iban": TEST_IBAN_WISE, "bank": "WISE PAYMENTS LIMITED"}]
        gl = {"1030 Wise": {"iban": None, "bic": None}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"WISE PAYMENTS LIMITED": None}),
                         [("Account", "1030 Wise", "iban", TEST_IBAN_WISE)])

    def test_bank_account_without_iban_or_gl_account_is_skipped(self):
        bank_accounts = [
            {"name": "no iban", "account": "1040 X", "iban": None, "bank": "UBS Switzerland AG"},
            {"name": "no gl", "account": None, "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
        ]
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, {}, {"UBS Switzerland AG": None}), [])

    def test_two_bank_accounts_on_one_gl_account_give_one_fix(self):
        bank_accounts = [
            {"name": "a", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
            {"name": "b", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
        ]
        fixes = swiss_setup.payment_fixes(bank_accounts, {"1020 UBS": {"iban": None, "bic": None}}, {"UBS Switzerland AG": None})
        self.assertEqual(len(fixes), len(set(fixes)))
        self.assertEqual(len(fixes), 3)


if __name__ == "__main__":
    unittest.main()
