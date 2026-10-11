"""Offline tests for the pure parts of swiss-setup.py: no site, no network.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import os
import sys
import types
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

# bi_payroll's test_payroll_accounts holds BEXIO_ADDED, the account numbers the Salary Component fixtures link
# and a fresh chart lacks. The accounts table in swiss-setup.py must name exactly those (one source): the app's
# directory goes on the path so the comparison test can import the list.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(_path), "..", "apps", "bi_payroll")))
from bi_payroll.test_payroll_accounts import BEXIO_ADDED  # noqa: E402


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

    def test_lohnausweis_is_a_step(self):
        self.assertIn("lohnausweis", swiss_setup.STEPS)

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

    def test_payroll_accounts_is_a_step(self):
        self.assertIn("payroll_accounts", swiss_setup.STEPS)

    def test_accounts_is_a_step(self):
        self.assertIn("accounts", swiss_setup.STEPS)


class PayslipChangesTest(unittest.TestCase):
    # The Lohnabrechnung format is in place when it carries the file's HTML and is not disabled.
    IN_PLACE = {"html": "<p>slip</p>", "disabled": 0}

    def test_a_format_in_place_wants_no_change(self):
        self.assertEqual(swiss_setup.payslip_changes(self.IN_PLACE, "<p>slip</p>"), {})

    def test_changed_html_updates_the_format(self):
        self.assertEqual(swiss_setup.payslip_changes(self.IN_PLACE, "<p>new</p>"), {"html": "<p>new</p>"})

    def test_a_disabled_format_is_enabled_again(self):
        self.assertEqual(swiss_setup.payslip_changes({**self.IN_PLACE, "disabled": 1}, "<p>slip</p>"), {"disabled": 0})


class SetDoctypePropertyTest(unittest.TestCase):
    def test_one_args_dict_with_the_six_keys_of_frappe_16_51(self):
        with mock.patch.object(swiss_setup, "frappe") as frappe:
            swiss_setup.set_doctype_property("Salary Slip", "default_print_format", "Lohnabrechnung", "Data")
        frappe.make_property_setter.assert_called_once_with({
            "doctype": "Salary Slip",
            "doctype_or_field": "DocType",
            "fieldname": None,
            "property": "default_print_format",
            "value": "Lohnabrechnung",
            "property_type": "Data",
        })


class DefaultPrintFormatStepTest(unittest.TestCase):
    # payslip() and qrbill(): the format's own work is skipped when it is in place, the default never is
    def run_step(self, step, default, doc):
        # the format file reads as "<p>slip</p>", the Print Format row is doc: in place when they match
        with mock.patch.object(swiss_setup, "frappe") as frappe, \
                mock.patch("builtins.open", mock.mock_open(read_data="<p>slip</p>")), \
                mock.patch.object(swiss_setup, default) as set_default, \
                mock.patch.object(swiss_setup, "say"):
            frappe.db.exists.return_value = True
            frappe.get_doc.return_value = doc
            step()
        return frappe, set_default

    def test_payslip_with_the_format_in_place_still_sets_the_default(self):
        frappe, set_default = self.run_step(swiss_setup.payslip, "payslip_default", {"html": "<p>slip</p>", "disabled": 0})
        set_default.assert_called_once_with()

    def test_qrbill_with_the_format_in_place_still_sets_the_default(self):
        # qrbill() reads the row's fields as attributes, payslip() through get()
        row = types.SimpleNamespace(html="<p>slip</p>", disabled=0, margin_bottom=1)
        frappe, set_default = self.run_step(swiss_setup.qrbill, "qrbill_default", row)
        set_default.assert_called_once_with()


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


class HrStepTest(unittest.TestCase):
    def test_without_hr_user_the_step_skips_and_touches_nothing(self):
        # swiss-setup.sh with no argument runs every step; hr must not end that run.
        said = []
        with mock.patch.dict(os.environ, {"HR_USER": ""}), \
                mock.patch.object(swiss_setup, "say", said.append), \
                mock.patch.object(swiss_setup, "frappe") as fr:
            swiss_setup.hr()
        self.assertEqual(len(said), 1)
        self.assertIn("skipped", said[0])
        fr.db.exists.assert_not_called()
        fr.db.commit.assert_not_called()


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


class PayrollAccountsTest(unittest.TestCase):
    # the step imports bi_payroll.swiss_rates at run time: the app's directory is on the path for these tests
    def setUp(self):
        bi_payroll = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "bi_payroll")
        sys.path.insert(0, os.path.abspath(bi_payroll))
        self.addCleanup(sys.path.remove, os.path.abspath(bi_payroll))

    def run_step(self, current):
        # the Company's default payroll payable as it is, and the account the step resolves 1091 to
        with mock.patch.object(swiss_setup, "frappe") as frappe, \
                mock.patch.object(swiss_setup, "account", return_value="1091 - Lohndurchlaufkonto - BIC") as account, \
                mock.patch.object(swiss_setup, "say") as say:
            frappe.db.get_value.return_value = current
            swiss_setup.payroll_accounts()
        return frappe, account, say

    def test_sets_the_default_payable_to_1091(self):
        frappe, account, say = self.run_step(current=None)
        account.assert_called_once_with("1091")
        frappe.db.set_value.assert_called_once_with(
            "Company", swiss_setup.COMPANY, "default_payroll_payable_account", "1091 - Lohndurchlaufkonto - BIC")
        frappe.db.commit.assert_called_once()
        self.assertIn("set to 1091", say.call_args[0][0])

    def test_a_second_run_sets_nothing(self):
        frappe, _, say = self.run_step(current="1091 - Lohndurchlaufkonto - BIC")
        frappe.db.set_value.assert_not_called()
        self.assertIn("already set", say.call_args[0][0])


# The bi_payroll fixtures: the components the Lohnausweis mapping names must exist there, under the same abbr.
PAYROLL_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "bi_payroll", "bi_payroll",
                                "fixtures", "salary_component.json")


class LohnausweisTest(unittest.TestCase):
    def setUp(self):
        import json
        with open(PAYROLL_FIXTURES, encoding="utf-8") as f:
            self.fixtures = {row["name"]: row for row in json.load(f)}

    def test_each_table_names_the_components_of_its_form_line(self):
        self.assertEqual(swiss_setup.lohnausweis_components(), {
            "salary": ["Basic Salary", "Commission"],
            "deduction_ahv": ["AHV/IV/EO Employee", "ALV Employee", "NBU Employee"],
            "deduction_pension": ["BVG Employee"],
            "source_tax_deduction": ["Quellensteuer Employee"],
        })

    def test_line_1_names_every_earning_fixture(self):
        # The Wegleitung's Ziffer 1 (and the view's line 1): every earning of the slip, so a new Earning
        # component must join the mapping or the certificate's line 1 falls short of the view's.
        earnings = {name for name, row in self.fixtures.items() if row["type"] == "Earning"}
        self.assertEqual({name for name, _ in swiss_setup.LOHNAUSWEIS["salary"]}, earnings)

    def test_every_pair_is_a_fixture_with_that_abbr(self):
        # The name is what the step matches, the abbr what the collision warning looks for: both must be the fixture's.
        for pairs in swiss_setup.LOHNAUSWEIS.values():
            for name, abbr in pairs:
                self.assertEqual(self.fixtures[name]["salary_component_abbr"], abbr)

    def test_fresh_settings_need_the_switch_and_every_table(self):
        wanted = swiss_setup.lohnausweis_components()
        turn_on, fields = swiss_setup.lohnausweis_changes(0, {}, wanted)
        self.assertTrue(turn_on)
        self.assertEqual(fields, list(wanted))

    def test_a_second_run_changes_nothing(self):
        wanted = swiss_setup.lohnausweis_components()
        self.assertEqual(swiss_setup.lohnausweis_changes(1, wanted, wanted), (False, []))

    def test_a_table_changed_by_hand_is_set_back(self):
        wanted = swiss_setup.lohnausweis_components()
        current = dict(wanted, salary=["Basic Salary", "Some other earning"])
        self.assertEqual(swiss_setup.lohnausweis_changes(1, current, wanted), (False, ["salary"]))

    def test_tables_outside_the_mapping_are_not_looked_at(self):
        # Lines the mapping does not fill (2.1, 13 ...) are left as they are: the list of changes names only the four.
        wanted = swiss_setup.lohnausweis_components()
        current = dict(wanted, catering=["Anything"])
        self.assertEqual(swiss_setup.lohnausweis_changes(1, current, wanted), (False, []))
    def test_expenses_is_a_step(self):
        self.assertIn("expenses", swiss_setup.STEPS)


class ClaimTypeRowsTest(unittest.TestCase):
    # The five bexio Spesen types (numbers and labels, no company data).
    WANTED = [
        ("5811", "Weiterbildung"),
        ("5820", "Reisespesen"),
        ("5821", "Verpflegungsspesen"),
        ("5822", "Übernachtungsspesen"),
        ("5832", "Pauschalspesen"),
    ]
    COMPANY = "Test BI Concepts AG"

    def test_the_five_types_are_58xx_accounts_with_unique_names(self):
        numbers = [number for number, _name in swiss_setup.EXPENSE_CLAIM_TYPES]
        names = [name for _number, name in swiss_setup.EXPENSE_CLAIM_TYPES]
        self.assertEqual(len(swiss_setup.EXPENSE_CLAIM_TYPES), 5)
        self.assertTrue(all(number.startswith("58") for number in numbers), numbers)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(swiss_setup.EXPENSE_CLAIM_TYPES, self.WANTED)

    def test_a_fresh_site_wants_every_row(self):
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, set())
        self.assertEqual(rows, [(name, self.COMPANY, number) for number, name in self.WANTED])

    def test_nothing_missing_when_every_type_points_at_its_account(self):
        existing = {(name, self.COMPANY, number) for number, name in self.WANTED}
        self.assertEqual(swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, existing), [])

    def existing_with(self, name, number):
        """The full site set, with one type's account replaced."""
        rows = {(n, self.COMPANY, num) for num, n in self.WANTED if n != name}
        return rows | {(name, self.COMPANY, number)}

    def test_a_type_on_the_wrong_account_is_written_again(self):
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, self.existing_with("Reisespesen", "6900"))
        self.assertEqual(rows, [("Reisespesen", self.COMPANY, "5820")])

    def test_a_row_of_another_company_is_no_row_of_this_one(self):
        existing = {(n, "Other AG", number) for number, n in self.WANTED}
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, existing)
        self.assertEqual(rows, [(name, self.COMPANY, number) for number, name in self.WANTED])

    def test_a_type_without_an_account_number_is_written_again(self):
        # A claim type whose account row has no account number never matches.
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, self.existing_with("Pauschalspesen", None))
        self.assertEqual(rows, [("Pauschalspesen", self.COMPANY, "5832")])

    def test_rows_come_in_the_table_s_order(self):
        # Whatever order the site holds them in, the writes follow EXPENSE_CLAIM_TYPES.
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, self.existing_with("Reisespesen", "6900"))
        self.assertEqual([name for name, _company, _number in rows], ["Reisespesen"])


class LohnausweisSiteTest(unittest.TestCase):
    # The step on a fake site. The live-era component 'Basic' holds abbr B, so the fixture's 'Basic Salary' got B_1.
    # Invented names and abbrs stand in for the copy's; the tables must name 'Basic Salary' and never 'Basic'.
    def run_step(self, components, settings_rows=None):
        # components: name -> abbr of every Salary Component on the site
        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch.object(swiss_setup, "say") as say:
            frappe.db.exists.side_effect = lambda doctype, name: name in components
            frappe.db.get_value.side_effect = lambda doctype, name, field: components[name]
            frappe.get_all.side_effect = lambda doctype, filters, pluck: [
                name for name, abbr in components.items()
                if abbr == filters["salary_component_abbr"] and name != filters["name"][1]]
            frappe.db.sql.return_value = [("ADDRESS-1",)]
            settings = frappe.get_single.return_value
            settings.dynamic_config = 0
            settings.get.side_effect = lambda field: (settings_rows or {}).get(field, [])
            swiss_setup.lohnausweis()
        return settings, say

    def fixture_site(self, **overrides):
        # every fixture component the mapping names, under its fixture abbr, unless overridden
        site = {name: abbr for pairs in swiss_setup.LOHNAUSWEIS.values() for name, abbr in pairs}
        site.update(overrides)
        return site

    def test_the_tables_name_the_fixture_components_not_the_colliding_one(self):
        site = self.fixture_site(**{"Basic Salary": "B_1", "Basic": "B"})
        settings, _ = self.run_step(site)
        salary = [call.args[1] for call in settings.set.call_args_list if call.args[0] == "salary"]
        self.assertEqual(salary, [[{"component": "Basic Salary"}, {"component": "Commission"}]])
        for call in settings.set.call_args_list:
            self.assertNotIn({"component": "Basic"}, call.args[1])

    def test_a_collision_is_printed_with_both_components(self):
        site = self.fixture_site(**{"Basic Salary": "B_1", "Basic": "B"})
        _, say = self.run_step(site)
        say.assert_any_call("lohnausweis: abbr B is held by 'Basic', the fixture's 'Basic Salary' has B_1")

    def test_no_collision_no_warning(self):
        _, say = self.run_step(self.fixture_site())
        self.assertFalse(any("is held by" in call.args[0] for call in say.call_args_list))

    def test_missing_fixture_sets_nothing(self):
        site = self.fixture_site()
        del site["Commission"]
        settings, say = self.run_step(site)
        settings.set.assert_not_called()
        say.assert_called_once_with("lohnausweis: no Salary Component named Commission; the bi_payroll fixtures are not installed")


class NewAccountsTest(unittest.TestCase):
    # The rows the accounts step inserts, derived from the table in swiss-setup.py.
    ROWS = {r["account_number"]: r for r in swiss_setup.new_accounts(swiss_setup.PAYROLL_ACCOUNTS)}

    def test_seven_rows_in_number_order(self):
        self.assertEqual([r["account_number"] for r in swiss_setup.new_accounts(swiss_setup.PAYROLL_ACCOUNTS)],
                         ["2270", "2271", "2273", "2274", "2279", "5003", "5720"])

    def test_the_report_type_follows_the_root_type(self):
        self.assertEqual(self.ROWS["2271"]["report_type"], "Balance Sheet")
        self.assertEqual(self.ROWS["5003"]["report_type"], "Profit and Loss")

    def test_the_parents_are_the_groups_the_live_chart_hangs_them_under(self):
        self.assertEqual((self.ROWS["2270"]["parent_number"], self.ROWS["2279"]["parent_number"]), ("2", "2"))
        self.assertEqual((self.ROWS["5003"]["parent_number"], self.ROWS["5720"]["parent_number"]), ("50", "57"))

    def test_the_names_are_the_mapping_labels(self):
        self.assertEqual(self.ROWS["2271"]["account_name"], "KK AHV, IV, EO, ALV")
        self.assertEqual(self.ROWS["5720"]["account_name"], "BVG-Beitrag")


class PayrollAccountsTableTest(unittest.TestCase):
    # One source: the fixtures' account links need exactly the bexio-added numbers (bi_payroll's BEXIO_ADDED),
    # and the accounts step makes exactly the table's numbers. If the two drift, a fresh migrate misses a link.
    def test_the_table_is_bexio_added(self):
        self.assertEqual(set(swiss_setup.PAYROLL_ACCOUNTS), set(BEXIO_ADDED))


class AccountsTest(unittest.TestCase):
    # The step runs in the container; here frappe is a mock: db.exists answers from a set, db.get_value gives the
    # Company's currency, and account() resolves the parent numbers or refuses one the chart has not.
    PARENTS = {"2": "Passiven - BIC", "50": "Lohnaufwand - BIC", "57": "Sozialversicherungsaufwand - BIC"}
    NUMBERS = ("2270", "2271", "2273", "2274", "2279", "5003", "5720")

    def run_step(self, existing=(), parents=PARENTS):
        existing = set(existing)

        def exists(doctype, filters):
            return doctype == "Account" and filters["account_number"] in existing

        def account(number):
            if number not in parents:
                raise RuntimeError(f"Account {number} not found in {swiss_setup.COMPANY}; run the coa step first")
            return parents[number]

        with mock.patch.object(swiss_setup, "frappe") as frappe, \
                mock.patch.object(swiss_setup, "account", side_effect=account), \
                mock.patch.object(swiss_setup, "say") as say:
            frappe.db.exists.side_effect = exists
            frappe.db.get_value.return_value = "CHF"
            swiss_setup.accounts()
        inserts = [c.args[0] for c in frappe.get_doc.call_args_list]
        return inserts, say, frappe

    def test_creates_the_seven_missing_accounts(self):
        inserts, say, frappe = self.run_step()
        self.assertEqual([d["account_number"] for d in inserts], list(self.NUMBERS))
        self.assertEqual(inserts[0], {
            "doctype": "Account", "account_name": "KK Vorsorgeeinrichtung", "company": swiss_setup.COMPANY,
            "parent_account": "Passiven - BIC", "account_number": "2270", "is_group": 0,
            "root_type": "Liability", "report_type": "Balance Sheet", "account_currency": "CHF",
        })
        frappe.db.commit.assert_called_once()
        self.assertIn("accounts: 2270 created", say.call_args_list[0].args[0])

    def test_skips_what_exists(self):
        inserts, say, _ = self.run_step(existing=["2271"])
        self.assertEqual([d["account_number"] for d in inserts], [n for n in self.NUMBERS if n != "2271"])
        self.assertTrue(any("2271 in place" in c.args[0] for c in say.call_args_list))

    def test_a_second_run_creates_nothing(self):
        inserts, say, frappe = self.run_step(existing=self.NUMBERS)
        self.assertEqual(inserts, [])
        frappe.db.commit.assert_called_once()
        self.assertEqual(say.call_count, len(self.NUMBERS))

    def test_a_missing_parent_group_is_refused_not_made(self):
        with self.assertRaises(RuntimeError):
            self.run_step(parents={})


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
