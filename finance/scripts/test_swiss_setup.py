"""Offline tests for the pure parts of swiss-setup.py: no site, no network.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import ast
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
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
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(_path), "..", "apps", "bi_payroll"))
)
from bi_payroll.test_payroll_accounts import BEXIO_ADDED  # noqa: E402


class SwissSetupProjectTest(unittest.TestCase):
    def run_wrapper(self, project=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            docker = tmp_path / "docker"
            docker.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$DOCKER_ARGS_FILE"\ncat >/dev/null\n',
                encoding="utf-8",
            )
            docker.chmod(0o755)
            args_file = tmp_path / "docker-args"
            env = {
                "PATH": f"{tmp}:{os.environ['PATH']}",
                "DOCKER_ARGS_FILE": str(args_file),
            }
            patch = mock.patch.dict(os.environ, env, clear=False)
            with patch:
                if project is None:
                    os.environ.pop("FINANCE_PROJECT", None)
                else:
                    os.environ["FINANCE_PROJECT"] = project
                subprocess.run(
                    [
                        "sh",
                        os.path.join(os.path.dirname(_path), "swiss-setup.sh"),
                        "hr",
                    ],
                    check=True,
                    stdin=subprocess.DEVNULL,
                )
            return args_file.read_text(encoding="utf-8").splitlines()

    def test_wrapper_defaults_to_live_project(self):
        args = self.run_wrapper()
        self.assertEqual(args[args.index("-p") + 1], "frappe-finance")

    def test_wrapper_uses_configured_project(self):
        args = self.run_wrapper("frappe-finance-copy")
        self.assertEqual(args[args.index("-p") + 1], "frappe-finance-copy")


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
        self.assertEqual(
            swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]), []
        )

    def test_empty_when_nothing_may_delete(self):
        # A doctype where no role but the kept one can delete: nothing to revoke.
        self.assertEqual(
            swiss_setup.delete_revokes(
                [("Sales User", 0, 0)], keep=["Accounts Manager"]
            ),
            [],
        )

    def test_revokes_on_every_permlevel(self):
        # The kept role is matched by name, whatever the permlevel.
        perms = [("Accounts User", 1, 1)]
        self.assertEqual(
            swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]),
            [("Accounts User", 1)],
        )


class ParseFreezeTest(unittest.TestCase):
    def test_date_alone_is_a_dry_run(self):
        self.assertEqual(
            swiss_setup.parse_freeze(["2025-12-31"]), ("2025-12-31", False)
        )

    def test_apply_flag_after_the_date(self):
        self.assertEqual(
            swiss_setup.parse_freeze(["2025-12-31", "--apply"]), ("2025-12-31", True)
        )

    def test_apply_flag_before_the_date(self):
        self.assertEqual(
            swiss_setup.parse_freeze(["--apply", "2025-12-31"]), ("2025-12-31", True)
        )

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
        self.assertIn(
            "apps/bi_payroll/bi_payroll/lohnabrechnung.html", swiss_setup.PAYSLIP_HTML
        )

    def test_hr_is_a_step(self):
        self.assertIn("hr", swiss_setup.STEPS)

    def test_payroll_settings_want_holds_benchis_decisions(self):
        # Benchi 2026-10-11: leave based, holidays counted, no slip e-mails
        # (Salary Slip.on_submit mails the slip when the check is on).
        self.assertEqual(
            swiss_setup.PAYROLL_SETTINGS_WANT,
            {
                "payroll_based_on": "Leave",
                "include_holidays_in_total_working_days": 1,
                "email_salary_slip_to_employee": 0,
            },
        )

    def test_hr_settings_wants_nothing(self):
        # HRMS v16.50.0 has no switch for self-service or onboarding e-mails;
        # the step must not invent one.
        self.assertEqual(swiss_setup.HR_SETTINGS_WANT, {})

    def test_the_wanted_roles_are_the_goal_four(self):
        self.assertEqual(
            swiss_setup.HR_ROLES,
            ["HR Manager", "HR User", "Payroll Manager", "Leave Approver"],
        )

    def test_payroll_accounts_is_a_step(self):
        self.assertIn("payroll_accounts", swiss_setup.STEPS)

    def test_accounts_is_a_step(self):
        self.assertIn("accounts", swiss_setup.STEPS)


class GebuevVersionedTest(unittest.TestCase):
    # gebuev() on a fake site: Salary Slip gets Track Changes, and no delete change.
    def run_step(self, tracked):
        # tracked: doctypes whose meta reads track_changes 1. The step adds to it, as
        # a Property Setter does; the DocType row keeps 0, as it does on a real site.
        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch.object(
            swiss_setup, "set_doctype_property"
        ) as set_property, mock.patch.object(swiss_setup, "say") as say:
            frappe.get_single.return_value.get.side_effect = lambda f: {
                "enable_immutable_ledger": 1,
                "delete_linked_ledger_entries": 0,
            }[f]
            # Every doctype has a role that may delete, so the step shows which it revokes.
            frappe.get_meta.side_effect = lambda dt: types.SimpleNamespace(
                track_changes=int(dt in tracked),
                permissions=[
                    types.SimpleNamespace(role="Some Role", permlevel=0, delete=1)
                ],
            )
            frappe.db.get_value.return_value = 0
            set_property.side_effect = lambda dt, *args: tracked.add(dt)
            swiss_setup.gebuev()
        return frappe, set_property, say

    def test_salary_slip_is_versioned_only(self):
        self.assertEqual(swiss_setup.GEBUEV_VERSIONED, ["Salary Slip"])
        self.assertNotIn("Salary Slip", swiss_setup.GEBUEV_TRACKED)

    def test_salary_slip_gets_track_changes_and_keeps_delete(self):
        frappe, set_property, _ = self.run_step(set(swiss_setup.GEBUEV_TRACKED))
        set_property.assert_called_once_with("Salary Slip", "track_changes", 1, "Check")
        revoked = frappe.permissions.update_permission_property.call_args_list
        self.assertEqual({c.args[0] for c in revoked}, set(swiss_setup.GEBUEV_TRACKED))

    def test_a_second_run_reports_nothing_to_do(self):
        tracked = set(swiss_setup.GEBUEV_TRACKED)
        self.run_step(tracked)
        _, set_property, say = self.run_step(tracked)
        set_property.assert_not_called()
        self.assertFalse(
            any("track changes on" in c.args[0] for c in say.call_args_list)
        )


class KeyRolesTest(unittest.TestCase):
    def test_key_roles_are_the_sync_roles_of_create_api_user(self):
        script = (pathlib.Path(_path).parent / "create-api-user.sh").read_text()
        line = next(l for l in script.splitlines() if l.startswith("SYNC_ROLES="))
        self.assertEqual(
            swiss_setup.KEY_ROLES, line.split("=", 1)[1].strip('"').split("|")
        )

    def test_the_key_check_holds_the_same_key_roles(self):
        check = pathlib.Path(_path).parents[1] / "bexio" / "api_key_check.py"
        node = next(
            n.value
            for n in ast.parse(check.read_text()).body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "KEY_ROLES" for t in n.targets)
        )
        self.assertEqual(swiss_setup.KEY_ROLES, ast.literal_eval(node))

    def test_the_mcp_roles_are_a_subset_of_the_key_roles(self):
        script = (pathlib.Path(_path).parent / "create-api-user.sh").read_text()
        line = next(l for l in script.splitlines() if l.startswith("MCP_ROLES="))
        mcp = line.split("=", 1)[1].strip('"').split("|")
        self.assertTrue(set(mcp) <= set(swiss_setup.KEY_ROLES))

    def test_the_key_check_asks_the_reports_keyaccess_closes(self):
        check = pathlib.Path(_path).parents[1] / "bexio" / "api_key_check.py"
        # Read the assignment, not a source line: the formatter spreads the list over several.
        node = next(
            n.value
            for n in ast.parse(check.read_text()).body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "REPORTS" for t in n.targets)
        )
        self.assertEqual(swiss_setup.KEY_REPORTS, ast.literal_eval(node))

    def test_keyaccess_is_a_step_and_named_in_the_usage_line(self):
        self.assertIn("keyaccess", swiss_setup.STEPS)
        wrapper = (pathlib.Path(_path).parent / "swiss-setup.sh").read_text()
        self.assertIn(" keyaccess ", wrapper)


class EmployeeRowsToRemoveTest(unittest.TestCase):
    # (role, permlevel, read, select, if_owner)
    def test_select_only_rows_of_key_roles_go(self):
        perms = [
            ("Accounts User", 0, 0, 1, 0),
            ("Sales Master Manager", 0, 0, 1, 0),
            ("HR User", 0, 1, 1, 0),
            ("Accounts Manager", 0, 1, 1, 0),
        ]
        self.assertEqual(
            swiss_setup.employee_rows_to_remove(perms, swiss_setup.KEY_ROLES),
            [("Accounts User", 0), ("Sales Master Manager", 0)],
        )

    def test_the_rows_if_owner_goes_along(self):
        self.assertEqual(
            swiss_setup.employee_rows_to_remove(
                [("Accounts User", 0, 0, 1, 1)], ["Accounts User"]
            ),
            [("Accounts User", 1)],
        )

    def test_a_row_at_another_permlevel_or_without_select_stays(self):
        perms = [("Accounts User", 1, 0, 1, 0), ("Accounts User", 0, 0, 0, 0)]
        self.assertEqual(
            swiss_setup.employee_rows_to_remove(perms, swiss_setup.KEY_ROLES), []
        )

    def test_nothing_to_remove_on_a_second_run(self):
        perms = [("Accounts Manager", 0, 1, 1, 0), ("HR User", 0, 1, 1, 0)]
        self.assertEqual(
            swiss_setup.employee_rows_to_remove(perms, swiss_setup.KEY_ROLES), []
        )

    def test_a_key_role_with_read_stops_and_is_named(self):
        perms = [("Accounts User", 0, 0, 1, 0), ("Item Manager", 0, 1, 1, 0)]
        with self.assertRaisesRegex(SystemExit, "Item Manager") as stop:
            swiss_setup.employee_rows_to_remove(perms, swiss_setup.KEY_ROLES)
        self.assertNotIn("Accounts User", str(stop.exception))


class ReportRolesTest(unittest.TestCase):
    def test_the_key_roles_leave_and_the_order_stays(self):
        self.assertEqual(
            swiss_setup.report_roles(
                "R", ["Accounts Manager", "Accounts User", "Auditor"], ["Accounts User"]
            ),
            ["Accounts Manager", "Auditor"],
        )

    def test_a_role_listed_twice_is_listed_once(self):
        self.assertEqual(
            swiss_setup.report_roles("R", ["A", "A"], ["B"]),
            ["A"],
        )

    def test_no_role_left_stops_and_names_the_report(self):
        with self.assertRaisesRegex(SystemExit, "R2"):
            swiss_setup.report_roles("R2", ["Accounts User"], swiss_setup.KEY_ROLES)

    def test_a_report_without_roles_stops_too(self):
        with self.assertRaises(SystemExit):
            swiss_setup.report_roles("R", [], swiss_setup.KEY_ROLES)


class LeaveLedgerTest(unittest.TestCase):
    def test_the_all_row_with_if_owner_is_found(self):
        self.assertTrue(swiss_setup.leave_ledger_all_row([("All", 0, 1)]))

    def test_other_all_rows_are_not_it(self):
        perms = [("All", 1, 1), ("All", 0, 0), ("HR User", 0, 1)]
        self.assertFalse(swiss_setup.leave_ledger_all_row(perms))

    def test_the_three_hr_roles_match(self):
        perms = [("System Manager", 0, 0), ("HR Manager", 0, 0), ("HR User", 0, 0)]
        self.assertEqual(swiss_setup.leave_ledger_mismatch(perms), [])

    def test_a_missing_or_extra_role_at_level_0_is_a_mismatch(self):
        perms = [("System Manager", 0, 0), ("HR Manager", 0, 0), ("All", 0, 1)]
        self.assertEqual(swiss_setup.leave_ledger_mismatch(perms), ["All", "HR User"])

    def test_rows_at_other_levels_do_not_count(self):
        perms = [
            ("System Manager", 0, 0),
            ("HR Manager", 0, 0),
            ("HR User", 0, 0),
            ("All", 1, 0),
        ]
        self.assertEqual(swiss_setup.leave_ledger_mismatch(perms), [])


class ProposalHelpersTest(unittest.TestCase):
    def levels(self, over=None):
        levels = {
            (dt, f): 0 for dt, fs in swiss_setup.PROPOSAL_FIELDS.items() for f in fs
        }
        levels.update(over or {})
        return levels

    def test_fields_to_raise_are_those_not_at_level_1_in_order(self):
        levels = self.levels()
        levels["Payment Proposal", "salaries"] = 1
        raised = swiss_setup.proposal_fields_to_raise(levels)
        self.assertNotIn(("Payment Proposal", "salaries"), raised)
        self.assertEqual(raised[0], ("Payment Proposal", "expenses"))
        self.assertEqual(len(raised), 12)

    def test_nothing_to_raise_when_all_are_at_level_1(self):
        levels = {k: 1 for k in self.levels()}
        self.assertEqual(swiss_setup.proposal_fields_to_raise(levels), [])

    def test_an_unknown_field_stops(self):
        with self.assertRaisesRegex(SystemExit, "Payment Proposal.expenses"):
            swiss_setup.proposal_fields_to_raise(
                self.levels({("Payment Proposal", "expenses"): None})
            )

    def test_rows_to_add_for_both_roles_when_there_is_no_level_1(self):
        perms = [("Accounts User", 0, 1, 1, 0), ("Accounts Manager", 0, 1, 1, 0)]
        self.assertEqual(
            swiss_setup.proposal_rows_to_add(perms),
            [
                ("Accounts Manager", ["read", "write"]),
                ("System Manager", ["read", "write"]),
            ],
        )

    def test_rows_to_add_completes_a_level_1_row_that_lacks_write(self):
        perms = [
            ("Accounts Manager", 1, 1, 0, 0),
            ("System Manager", 1, 1, 1, 0),
        ]
        self.assertEqual(
            swiss_setup.proposal_rows_to_add(perms), [("Accounts Manager", ["write"])]
        )

    def test_rows_to_add_is_empty_when_both_roles_hold_level_1(self):
        perms = [("Accounts Manager", 1, 1, 1, 0), ("System Manager", 1, 1, 1, 0)]
        self.assertEqual(swiss_setup.proposal_rows_to_add(perms), [])

    def test_a_key_role_at_level_1_stops_and_level_0_does_not(self):
        swiss_setup.proposal_stops(
            [("Accounts User", 0, 1, 1, 0)], swiss_setup.KEY_ROLES
        )
        with self.assertRaisesRegex(SystemExit, "Item Manager"):
            swiss_setup.proposal_stops(
                [("Item Manager", 1, 1, 0, 0)], swiss_setup.KEY_ROLES
            )

    def test_users_stop_names_the_count_and_no_user(self):
        with self.assertRaises(SystemExit) as stop:
            swiss_setup.proposal_users_stop(
                [["Accounts User"], ["Accounts User", "Sales User"], ["HR User"]]
            )
        self.assertIn("2 enabled user(s)", str(stop.exception))

    def test_a_manager_or_system_manager_with_accounts_user_does_not_stop(self):
        swiss_setup.proposal_users_stop(
            [["Accounts User", "Accounts Manager"], ["Accounts User", "System Manager"]]
        )


class FakeKeySite:
    """The permission rows, report roles and Custom Roles the keyaccess step reads and writes."""

    def __init__(self):
        self.perms = {
            "Employee": [
                ("Accounts User", 0, 0, 1, 0),
                ("Sales Master Manager", 0, 0, 1, 0),
                ("Accounts Manager", 0, 1, 1, 0),
                ("HR User", 0, 1, 1, 0),
            ],
            swiss_setup.LEAVE_LEDGER: [
                ("System Manager", 0, 0, 0, 0),
                ("HR Manager", 0, 0, 0, 0),
                ("HR User", 0, 0, 0, 0),
                ("All", 0, 0, 0, 1),
            ],
            swiss_setup.PROPOSAL: [
                ("Accounts Manager", 0, 1, 1, 0),
                ("System Manager", 0, 1, 1, 0),
                ("Accounts User", 0, 1, 1, 0),
            ],
        }
        self.levels = {
            (dt, f): 0 for dt, fs in swiss_setup.PROPOSAL_FIELDS.items() for f in fs
        }
        self.users = {
            "someone@example.com": ["Accounts User", "Accounts Manager"],
            "api-mcp@finance.local": ["Accounts User"],
        }
        self.setters = []
        self.has_role = {
            "Cash Flow Forecast": ["Accounts Manager", "Accounts User"],
            "Cash Flow Forecast Lines": ["Accounts Manager", "Accounts User"],
            "Project Profitability": ["Projects Manager", "Accounts User"],
            "Monthly Worktime": ["HR Manager", "Accounts User"],
            "Worktime Overview": ["HR Manager", "Accounts User"],
        }
        self.custom = {}
        self.removed = []
        self.commits = 0

    def remove(self, doctype, role, permlevel, if_owner=0):
        self.removed.append((doctype, role))
        self.perms[doctype] = [
            p
            for p in self.perms[doctype]
            if not (p[0] == role and p[1] == permlevel and p[-1] == if_owner)
        ]

    def meta(self, doctype):
        fields = {
            "Employee": ("role", "permlevel", "read", "select", "if_owner"),
            swiss_setup.LEAVE_LEDGER: ("role", "permlevel", "x", "y", "if_owner"),
            swiss_setup.PROPOSAL: ("role", "permlevel", "read", "write", "if_owner"),
        }.get(doctype)
        rows = [
            types.SimpleNamespace(get=dict(zip(fields, p)).get)
            for p in self.perms.get(doctype, [])
        ]

        def get_field(name):
            if (doctype, name) not in self.levels:
                return None
            return types.SimpleNamespace(permlevel=self.levels[doctype, name])

        return types.SimpleNamespace(permissions=rows, get_field=get_field)

    def make_property_setter(self, args):
        self.setters.append((args["doctype"], args["fieldname"], args["property"]))
        self.levels[args["doctype"], args["fieldname"]] = args["value"]

    def add_permission(self, doctype, role, permlevel=0, ptype=None):
        if not any(p[0] == role and p[1] == permlevel for p in self.perms[doctype]):
            self.perms[doctype].append((role, permlevel, 1, 0, 0))

    def update_permission_property(self, doctype, role, permlevel, ptype, value):
        index = {"read": 2, "write": 3}[ptype]
        self.perms[doctype] = [
            (
                p[:index] + (value,) + p[index + 1 :]
                if p[0] == role and p[1] == permlevel
                else p
            )
            for p in self.perms[doctype]
        ]

    def custom_role(self, name=None):
        site = self

        class Doc:
            def __init__(self, report=None):
                self.report = report
                self.roles = [
                    types.SimpleNamespace(role=r) for r in site.custom.get(report, [])
                ]

            def set(self, _field, rows):
                self.roles = [types.SimpleNamespace(role=r["role"]) for r in rows]

            def save(self):
                site.custom[self.report] = [d.role for d in self.roles]

            insert = save

        return Doc(name)

    def run(self):
        remove_module = types.SimpleNamespace(remove=self.remove)
        name = "frappe.core.page.permission_manager.permission_manager"
        with mock.patch.dict(sys.modules, {name: remove_module}), mock.patch.object(
            swiss_setup, "frappe"
        ) as frappe, mock.patch.object(swiss_setup, "say") as say:
            frappe.get_meta.side_effect = self.meta
            frappe.make_property_setter.side_effect = self.make_property_setter
            frappe.permissions.add_permission.side_effect = self.add_permission
            frappe.permissions.update_permission_property.side_effect = (
                self.update_permission_property
            )
            frappe.db.exists.return_value = True

            def get_all(dt, filters, **kw):
                if dt == "User":
                    return list(self.users)
                if dt == "Has Role" and filters["parenttype"] == "User":
                    return list(self.users[filters["parent"]])
                return list(self.has_role[filters["parent"]])

            frappe.get_all.side_effect = get_all
            frappe.db.get_value.side_effect = lambda _dt, filters, _f: (
                filters["report"] if filters["report"] in self.custom else None
            )
            frappe.get_doc.side_effect = lambda _dt, name: self.custom_role(name)
            frappe.new_doc.side_effect = lambda _dt: self.custom_role()
            frappe.db.commit.side_effect = lambda: setattr(
                self, "commits", self.commits + 1
            )
            swiss_setup.keyaccess()
        return [c.args[0] for c in say.call_args_list]


class KeyaccessTest(unittest.TestCase):
    def test_it_closes_employee_the_leave_ledger_and_both_reports(self):
        site = FakeKeySite()
        site.run()
        self.assertEqual(
            site.removed,
            [
                ("Employee", "Accounts User"),
                ("Employee", "Sales Master Manager"),
                ("Leave Ledger Entry", "All"),
            ],
        )
        self.assertEqual(site.custom["Cash Flow Forecast"], ["Accounts Manager"])
        self.assertEqual(site.custom["Cash Flow Forecast Lines"], ["Accounts Manager"])
        self.assertEqual(site.custom["Project Profitability"], ["Projects Manager"])
        self.assertEqual(site.custom["Monthly Worktime"], ["HR Manager"])
        self.assertEqual(site.custom["Worktime Overview"], ["HR Manager"])
        self.assertEqual(site.commits, 1)

    def test_it_raises_the_proposal_tables_and_their_child_fields_to_level_1(self):
        site = FakeKeySite()
        said = site.run()
        wanted = {(dt, f) for dt, fs in swiss_setup.PROPOSAL_FIELDS.items() for f in fs}
        self.assertEqual({(d, f) for d, f, _p in site.setters}, wanted)
        self.assertEqual(set(site.levels.values()), {1})
        self.assertTrue(
            any("Payment Proposal.salaries: permlevel set to 1" in l for l in said)
        )

    def test_level_1_rows_hold_read_and_write_and_level_0_stays(self):
        site = FakeKeySite()
        site.run()
        rows = site.perms[swiss_setup.PROPOSAL]
        self.assertIn(("Accounts Manager", 1, 1, 1, 0), rows)
        self.assertIn(("System Manager", 1, 1, 1, 0), rows)
        self.assertEqual(
            {r for r, level, *_ in rows if level == 1},
            {"Accounts Manager", "System Manager"},
        )
        self.assertIn(("Accounts User", 0, 1, 1, 0), rows)

    def test_a_key_role_with_level_1_on_the_proposal_stops_before_any_change(self):
        site = FakeKeySite()
        site.perms[swiss_setup.PROPOSAL].append(("Stock User", 1, 1, 0, 0))
        with self.assertRaisesRegex(SystemExit, "Stock User"):
            site.run()
        self.assertEqual((site.removed, site.setters, site.custom), ([], [], {}))

    def test_a_human_with_accounts_user_only_stops_before_any_change(self):
        site = FakeKeySite()
        site.users["clerk@example.com"] = ["Accounts User"]
        with self.assertRaisesRegex(SystemExit, "1 enabled user"):
            site.run()
        self.assertEqual((site.removed, site.setters, site.custom), ([], [], {}))

    def test_the_api_users_are_not_humans(self):
        # api-mcp holds Accounts User alone in the fake site, and the run does not stop.
        FakeKeySite().run()

    def test_a_field_the_site_does_not_have_stops_before_any_change(self):
        site = FakeKeySite()
        del site.levels["Payment Proposal Expense", "amount"]
        with self.assertRaisesRegex(SystemExit, "Payment Proposal Expense.amount"):
            site.run()
        self.assertEqual(site.removed, [])

    def test_it_leaves_the_other_employee_rows_alone(self):
        site = FakeKeySite()
        site.run()
        self.assertEqual(
            [p[0] for p in site.perms["Employee"]], ["Accounts Manager", "HR User"]
        )

    def test_a_second_run_changes_nothing_and_says_so(self):
        site = FakeKeySite()
        site.run()
        removed, custom = list(site.removed), dict(site.custom)
        setters, perms = list(site.setters), list(site.perms[swiss_setup.PROPOSAL])
        said = site.run()
        self.assertEqual(site.removed, removed)
        self.assertEqual(site.custom, custom)
        self.assertEqual(site.setters, setters)
        self.assertEqual(site.perms[swiss_setup.PROPOSAL], perms)
        self.assertTrue(any("nothing to change" in line for line in said))

    def test_an_existing_custom_role_is_updated(self):
        site = FakeKeySite()
        site.custom["Cash Flow Forecast"] = ["Accounts Manager", "Accounts User"]
        site.run()
        self.assertEqual(site.custom["Cash Flow Forecast"], ["Accounts Manager"])

    def test_a_stop_comes_before_any_change(self):
        site = FakeKeySite()
        site.perms["Employee"].append(("Item Manager", 0, 1, 1, 0))
        with self.assertRaisesRegex(SystemExit, "Item Manager"):
            site.run()
        self.assertEqual(site.removed, [])
        self.assertEqual(site.custom, {})

    def test_a_report_left_without_roles_stops_before_any_change(self):
        site = FakeKeySite()
        site.has_role["Cash Flow Forecast"] = ["Accounts User"]
        with self.assertRaisesRegex(SystemExit, "Cash Flow Forecast"):
            site.run()
        self.assertEqual(site.removed, [])

    def test_a_leave_ledger_that_differs_after_the_removal_fails_the_read_back(self):
        site = FakeKeySite()
        site.perms[swiss_setup.LEAVE_LEDGER].append(("Employee", 0, 0, 0, 0))
        with self.assertRaisesRegex(SystemExit, "read-back.*Employee"):
            site.run()

    def test_only_roles_and_report_names_are_printed(self):
        said = " ".join(FakeKeySite().run())
        self.assertIn("Accounts User", said)
        # field names are printed, never a user
        self.assertNotIn("example.com", said)
        self.assertNotIn("finance.local", said)


class PayslipChangesTest(unittest.TestCase):
    # The Lohnabrechnung format is in place when it carries the file's HTML and is not disabled.
    IN_PLACE = {"html": "<p>slip</p>", "disabled": 0}

    def test_a_format_in_place_wants_no_change(self):
        self.assertEqual(swiss_setup.payslip_changes(self.IN_PLACE, "<p>slip</p>"), {})

    def test_changed_html_updates_the_format(self):
        self.assertEqual(
            swiss_setup.payslip_changes(self.IN_PLACE, "<p>new</p>"),
            {"html": "<p>new</p>"},
        )

    def test_a_disabled_format_is_enabled_again(self):
        self.assertEqual(
            swiss_setup.payslip_changes(
                {**self.IN_PLACE, "disabled": 1}, "<p>slip</p>"
            ),
            {"disabled": 0},
        )


class SetDoctypePropertyTest(unittest.TestCase):
    def test_one_args_dict_with_the_six_keys_of_frappe_16_51(self):
        with mock.patch.object(swiss_setup, "frappe") as frappe:
            swiss_setup.set_doctype_property(
                "Salary Slip", "default_print_format", "Lohnabrechnung", "Data"
            )
        frappe.make_property_setter.assert_called_once_with(
            {
                "doctype": "Salary Slip",
                "doctype_or_field": "DocType",
                "fieldname": None,
                "property": "default_print_format",
                "value": "Lohnabrechnung",
                "property_type": "Data",
            }
        )


class DefaultPrintFormatStepTest(unittest.TestCase):
    # payslip() and qrbill(): the format's own work is skipped when it is in place, the default never is
    def run_step(self, step, default, doc):
        # the format file reads as "<p>slip</p>", the Print Format row is doc: in place when they match
        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch(
            "builtins.open", mock.mock_open(read_data="<p>slip</p>")
        ), mock.patch.object(swiss_setup, default) as set_default, mock.patch.object(
            swiss_setup, "say"
        ):
            frappe.db.exists.return_value = True
            frappe.get_doc.return_value = doc
            step()
        return frappe, set_default

    def test_payslip_with_the_format_in_place_still_sets_the_default(self):
        frappe, set_default = self.run_step(
            swiss_setup.payslip,
            "payslip_default",
            {"html": "<p>slip</p>", "disabled": 0},
        )
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
        to_add, not_offered = swiss_setup.hr_grants(
            self.WANTED, ["HR Manager", "HR User", "Leave Approver"], []
        )
        self.assertEqual(to_add, ["HR Manager", "HR User", "Leave Approver"])
        self.assertEqual(not_offered, ["Payroll Manager"])

    def test_nothing_to_do_when_all_in_place(self):
        self.assertEqual(
            swiss_setup.hr_grants(self.WANTED, self.WANTED, self.WANTED), ([], [])
        )


class HrStepTest(unittest.TestCase):
    def test_without_hr_user_the_step_skips_and_touches_nothing(self):
        # swiss-setup.sh with no argument runs every step; hr must not end that run.
        said = []
        with mock.patch.dict(os.environ, {"HR_USER": ""}), mock.patch.object(
            swiss_setup, "say", said.append
        ), mock.patch.object(swiss_setup, "frappe") as fr:
            swiss_setup.hr()
        self.assertEqual(len(said), 1)
        self.assertIn("skipped", said[0])
        fr.db.exists.assert_not_called()
        fr.db.commit.assert_not_called()


class SingleFixesTest(unittest.TestCase):
    def test_sets_only_offered_fields_that_differ(self):
        want = {
            "payroll_based_on": "Leave",
            "include_holidays_in_total_working_days": 1,
        }
        current = {
            "payroll_based_on": "Leave",
            "include_holidays_in_total_working_days": 0,
        }
        changes, not_offered = swiss_setup.single_fixes(want, current, set(want))
        self.assertEqual(changes, [("include_holidays_in_total_working_days", 0, 1)])
        self.assertEqual(not_offered, [])

    def test_an_already_right_value_changes_nothing(self):
        self.assertEqual(swiss_setup.single_fixes({"a": 0}, {"a": 0}, {"a"}), ([], []))

    def test_a_field_the_single_has_not_is_reported_not_set(self):
        changes, not_offered = swiss_setup.single_fixes(
            {"a": 1, "b": 2}, {"a": 1, "b": None}, {"a"}
        )
        self.assertEqual(changes, [])
        self.assertEqual(not_offered, ["b"])


class PayrollAccountsTest(unittest.TestCase):
    # the step imports bi_payroll.swiss_rates at run time: the app's directory is on the path for these tests
    def setUp(self):
        bi_payroll = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "apps", "bi_payroll"
        )
        sys.path.insert(0, os.path.abspath(bi_payroll))
        self.addCleanup(sys.path.remove, os.path.abspath(bi_payroll))

    def run_step(self, current):
        # the Company's default payroll payable as it is, and the account the step resolves 1091 to
        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch.object(
            swiss_setup, "account", return_value="1091 - Lohndurchlaufkonto - BIC"
        ) as account, mock.patch.object(swiss_setup, "say") as say:
            frappe.db.get_value.return_value = current
            swiss_setup.payroll_accounts()
        return frappe, account, say

    def test_sets_the_default_payable_to_1091(self):
        frappe, account, say = self.run_step(current=None)
        account.assert_called_once_with("1091")
        frappe.db.set_value.assert_called_once_with(
            "Company",
            swiss_setup.COMPANY,
            "default_payroll_payable_account",
            "1091 - Lohndurchlaufkonto - BIC",
        )
        frappe.db.commit.assert_called_once()
        self.assertIn("set to 1091", say.call_args[0][0])

    def test_a_second_run_sets_nothing(self):
        frappe, _, say = self.run_step(current="1091 - Lohndurchlaufkonto - BIC")
        frappe.db.set_value.assert_not_called()
        self.assertIn("already set", say.call_args[0][0])


# The bi_payroll fixtures: the components the Lohnausweis mapping names must exist there, under the same abbr.
PAYROLL_FIXTURES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "apps",
    "bi_payroll",
    "bi_payroll",
    "fixtures",
    "salary_component.json",
)


class LohnausweisTest(unittest.TestCase):
    def setUp(self):
        import json

        with open(PAYROLL_FIXTURES, encoding="utf-8") as f:
            self.fixtures = {row["name"]: row for row in json.load(f)}

    def test_each_table_names_the_components_of_its_form_line(self):
        self.assertEqual(
            swiss_setup.lohnausweis_components(),
            {
                "salary": ["Basic Salary", "Commission"],
                "deduction_ahv": ["AHV/IV/EO Employee", "ALV Employee", "NBU Employee"],
                "deduction_pension": ["BVG Employee"],
                "source_tax_deduction": ["Quellensteuer Employee"],
            },
        )

    def test_line_1_names_every_earning_fixture(self):
        # The Wegleitung's Ziffer 1 (and the view's line 1): every earning of the slip, so a new Earning
        # component must join the mapping or the certificate's line 1 falls short of the view's.
        earnings = {
            name
            for name, row in self.fixtures.items()
            if row["type"] == "Earning"
            and name
            not in {
                "Hourly Wage",
                "Vacation Compensation",
                "Expenses Effective",
                "Expenses Lump Sum",
            }
        }
        self.assertEqual(
            {name for name, _ in swiss_setup.LOHNAUSWEIS["salary"]}, earnings
        )

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
        self.assertEqual(
            swiss_setup.lohnausweis_changes(1, wanted, wanted), (False, [])
        )

    def test_a_table_changed_by_hand_is_set_back(self):
        wanted = swiss_setup.lohnausweis_components()
        current = dict(wanted, salary=["Basic Salary", "Some other earning"])
        self.assertEqual(
            swiss_setup.lohnausweis_changes(1, current, wanted), (False, ["salary"])
        )

    def test_tables_outside_the_mapping_are_not_looked_at(self):
        # Lines the mapping does not fill (2.1, 13 ...) are left as they are: the list of changes names only the four.
        wanted = swiss_setup.lohnausweis_components()
        current = dict(wanted, catering=["Anything"])
        self.assertEqual(
            swiss_setup.lohnausweis_changes(1, current, wanted), (False, [])
        )


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

    def test_expenses_is_a_step(self):
        self.assertIn("expenses", swiss_setup.STEPS)

    def test_the_five_types_are_58xx_accounts_with_unique_names(self):
        numbers = [number for number, _name in swiss_setup.EXPENSE_CLAIM_TYPES]
        names = [name for _number, name in swiss_setup.EXPENSE_CLAIM_TYPES]
        self.assertEqual(len(swiss_setup.EXPENSE_CLAIM_TYPES), 5)
        self.assertTrue(all(number.startswith("58") for number in numbers), numbers)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(swiss_setup.EXPENSE_CLAIM_TYPES, self.WANTED)

    def test_a_fresh_site_wants_every_row(self):
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, set())
        self.assertEqual(
            rows, [(name, self.COMPANY, number) for number, name in self.WANTED]
        )

    def test_nothing_missing_when_every_type_points_at_its_account(self):
        existing = {(name, self.COMPANY, number) for number, name in self.WANTED}
        self.assertEqual(
            swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, existing), []
        )

    def existing_with(self, name, number):
        """The full site set, with one type's account replaced."""
        rows = {(n, self.COMPANY, num) for num, n in self.WANTED if n != name}
        return rows | {(name, self.COMPANY, number)}

    def test_a_type_on_the_wrong_account_is_written_again(self):
        rows = swiss_setup.claim_type_rows(
            self.WANTED, self.COMPANY, self.existing_with("Reisespesen", "6900")
        )
        self.assertEqual(rows, [("Reisespesen", self.COMPANY, "5820")])

    def test_a_row_of_another_company_is_no_row_of_this_one(self):
        existing = {(n, "Other AG", number) for number, n in self.WANTED}
        rows = swiss_setup.claim_type_rows(self.WANTED, self.COMPANY, existing)
        self.assertEqual(
            rows, [(name, self.COMPANY, number) for number, name in self.WANTED]
        )

    def test_a_type_without_an_account_number_is_written_again(self):
        # A claim type whose account row has no account number never matches.
        rows = swiss_setup.claim_type_rows(
            self.WANTED, self.COMPANY, self.existing_with("Pauschalspesen", None)
        )
        self.assertEqual(rows, [("Pauschalspesen", self.COMPANY, "5832")])

    def test_rows_come_in_the_table_s_order(self):
        # Whatever order the site holds them in, the writes follow EXPENSE_CLAIM_TYPES.
        rows = swiss_setup.claim_type_rows(
            self.WANTED, self.COMPANY, self.existing_with("Reisespesen", "6900")
        )
        self.assertEqual([name for name, _company, _number in rows], ["Reisespesen"])


class LohnausweisSiteTest(unittest.TestCase):
    # The step on a fake site. The live-era component 'Basic' holds abbr B, so the fixture's 'Basic Salary' got B_1.
    # Invented names and abbrs stand in for the copy's; the tables must name 'Basic Salary' and never 'Basic'.
    def run_step(self, components, settings_rows=None):
        # components: name -> abbr of every Salary Component on the site
        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch.object(
            swiss_setup, "say"
        ) as say:
            frappe.db.exists.side_effect = lambda doctype, name: name in components
            frappe.db.get_value.side_effect = lambda doctype, name, field: components[
                name
            ]
            frappe.get_all.side_effect = lambda doctype, filters, pluck: [
                name
                for name, abbr in components.items()
                if abbr == filters["salary_component_abbr"]
                and name != filters["name"][1]
            ]
            frappe.db.sql.return_value = [("ADDRESS-1",)]
            settings = frappe.get_single.return_value
            settings.dynamic_config = 0
            settings.get.side_effect = lambda field: (settings_rows or {}).get(
                field, []
            )
            swiss_setup.lohnausweis()
        return settings, say

    def fixture_site(self, **overrides):
        # every fixture component the mapping names, under its fixture abbr, unless overridden
        site = {
            name: abbr
            for pairs in swiss_setup.LOHNAUSWEIS.values()
            for name, abbr in pairs
        }
        site.update(overrides)
        return site

    def test_the_tables_name_the_fixture_components_not_the_colliding_one(self):
        site = self.fixture_site(**{"Basic Salary": "B_1", "Basic": "B"})
        settings, _ = self.run_step(site)
        salary = [
            call.args[1]
            for call in settings.set.call_args_list
            if call.args[0] == "salary"
        ]
        self.assertEqual(
            salary, [[{"component": "Basic Salary"}, {"component": "Commission"}]]
        )
        for call in settings.set.call_args_list:
            self.assertNotIn({"component": "Basic"}, call.args[1])

    def test_a_collision_is_printed_with_both_components(self):
        site = self.fixture_site(**{"Basic Salary": "B_1", "Basic": "B"})
        _, say = self.run_step(site)
        say.assert_any_call(
            "lohnausweis: abbr B is held by 'Basic', the fixture's 'Basic Salary' has B_1"
        )

    def test_no_collision_no_warning(self):
        _, say = self.run_step(self.fixture_site())
        self.assertFalse(
            any("is held by" in call.args[0] for call in say.call_args_list)
        )

    def test_missing_fixture_sets_nothing(self):
        site = self.fixture_site()
        del site["Commission"]
        settings, say = self.run_step(site)
        settings.set.assert_not_called()
        say.assert_called_once_with(
            "lohnausweis: no Salary Component named Commission; the bi_payroll fixtures are not installed"
        )


class NewAccountsTest(unittest.TestCase):
    # The rows the accounts step inserts, derived from the table in swiss-setup.py.
    ROWS = {
        r["account_number"]: r
        for r in swiss_setup.new_accounts(swiss_setup.PAYROLL_ACCOUNTS)
    }

    def test_seven_rows_in_number_order(self):
        self.assertEqual(
            [
                r["account_number"]
                for r in swiss_setup.new_accounts(swiss_setup.PAYROLL_ACCOUNTS)
            ],
            ["2270", "2271", "2273", "2274", "2279", "5003", "5720"],
        )

    def test_the_report_type_follows_the_root_type(self):
        self.assertEqual(self.ROWS["2271"]["report_type"], "Balance Sheet")
        self.assertEqual(self.ROWS["5003"]["report_type"], "Profit and Loss")

    def test_the_parents_are_the_groups_the_live_chart_hangs_them_under(self):
        self.assertEqual(
            (self.ROWS["2270"]["parent_number"], self.ROWS["2279"]["parent_number"]),
            ("2", "2"),
        )
        self.assertEqual(
            (self.ROWS["5003"]["parent_number"], self.ROWS["5720"]["parent_number"]),
            ("50", "57"),
        )

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
    PARENTS = {
        "2": "Passiven - BIC",
        "50": "Lohnaufwand - BIC",
        "57": "Sozialversicherungsaufwand - BIC",
    }
    NUMBERS = ("2270", "2271", "2273", "2274", "2279", "5003", "5720")

    def run_step(self, existing=(), parents=PARENTS):
        existing = set(existing)

        def exists(doctype, filters):
            return doctype == "Account" and filters["account_number"] in existing

        def account(number):
            if number not in parents:
                raise RuntimeError(
                    f"Account {number} not found in {swiss_setup.COMPANY}; run the coa step first"
                )
            return parents[number]

        with mock.patch.object(swiss_setup, "frappe") as frappe, mock.patch.object(
            swiss_setup, "account", side_effect=account
        ), mock.patch.object(swiss_setup, "say") as say:
            frappe.db.exists.side_effect = exists
            frappe.db.get_value.return_value = "CHF"
            swiss_setup.accounts()
        inserts = [c.args[0] for c in frappe.get_doc.call_args_list]
        return inserts, say, frappe

    def test_creates_the_seven_missing_accounts(self):
        inserts, say, frappe = self.run_step()
        self.assertEqual([d["account_number"] for d in inserts], list(self.NUMBERS))
        self.assertEqual(
            inserts[0],
            {
                "doctype": "Account",
                "account_name": "KK Vorsorgeeinrichtung",
                "company": swiss_setup.COMPANY,
                "parent_account": "Passiven - BIC",
                "account_number": "2270",
                "is_group": 0,
                "root_type": "Liability",
                "report_type": "Balance Sheet",
                "account_currency": "CHF",
            },
        )
        frappe.db.commit.assert_called_once()
        self.assertIn("accounts: 2270 created", say.call_args_list[0].args[0])

    def test_skips_what_exists(self):
        inserts, say, _ = self.run_step(existing=["2271"])
        self.assertEqual(
            [d["account_number"] for d in inserts],
            [n for n in self.NUMBERS if n != "2271"],
        )
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
        bank_accounts = [
            {
                "name": "UBS KK",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            }
        ]
        fixes = swiss_setup.payment_fixes(
            bank_accounts,
            {"1020 UBS": {"iban": None, "bic": None}},
            {"UBS Switzerland AG": None},
        )
        self.assertEqual(
            fixes,
            [
                ("Account", "1020 UBS", "iban", TEST_IBAN),
                ("Bank", "UBS Switzerland AG", "swift_number", UBS_BIC),
                ("Account", "1020 UBS", "bic", UBS_BIC),
            ],
        )

    def test_second_run_changes_nothing(self):
        # Once the fixes are applied, the next run finds every field set.
        bank_accounts = [
            {
                "name": "UBS KK",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            }
        ]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": UBS_BIC}}
        self.assertEqual(
            swiss_setup.payment_fixes(
                bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}
            ),
            [],
        )

    def test_a_value_entered_by_hand_is_not_overwritten(self):
        bank_accounts = [
            {
                "name": "UBS KK",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            }
        ]
        gl = {"1020 UBS": {"iban": "CH0000000000000000009", "bic": "OTHERBIC"}}
        self.assertEqual(
            swiss_setup.payment_fixes(
                bank_accounts, gl, {"UBS Switzerland AG": "OTHERBIC"}
            ),
            [],
        )

    def test_bank_swift_number_kept_and_copied_to_gl_bic(self):
        # A Bank that already has a swift number keeps it; the GL Account takes it.
        bank_accounts = [
            {
                "name": "UBS KK",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            }
        ]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": None}}
        self.assertEqual(
            swiss_setup.payment_fixes(
                bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}
            ),
            [("Account", "1020 UBS", "bic", UBS_BIC)],
        )

    def test_bank_without_public_bic_gets_only_the_iban(self):
        # Wise has no BIC in the table: its IBAN is copied, nothing else is set.
        bank_accounts = [
            {
                "name": "Wise",
                "account": "1030 Wise",
                "iban": TEST_IBAN_WISE,
                "bank": "WISE PAYMENTS LIMITED",
            }
        ]
        gl = {"1030 Wise": {"iban": None, "bic": None}}
        self.assertEqual(
            swiss_setup.payment_fixes(
                bank_accounts, gl, {"WISE PAYMENTS LIMITED": None}
            ),
            [("Account", "1030 Wise", "iban", TEST_IBAN_WISE)],
        )

    def test_bank_account_without_iban_or_gl_account_is_skipped(self):
        bank_accounts = [
            {
                "name": "no iban",
                "account": "1040 X",
                "iban": None,
                "bank": "UBS Switzerland AG",
            },
            {
                "name": "no gl",
                "account": None,
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            },
        ]
        self.assertEqual(
            swiss_setup.payment_fixes(bank_accounts, {}, {"UBS Switzerland AG": None}),
            [],
        )

    def test_two_bank_accounts_on_one_gl_account_give_one_fix(self):
        bank_accounts = [
            {
                "name": "a",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            },
            {
                "name": "b",
                "account": "1020 UBS",
                "iban": TEST_IBAN,
                "bank": "UBS Switzerland AG",
            },
        ]
        fixes = swiss_setup.payment_fixes(
            bank_accounts,
            {"1020 UBS": {"iban": None, "bic": None}},
            {"UBS Switzerland AG": None},
        )
        self.assertEqual(len(fixes), len(set(fixes)))
        self.assertEqual(len(fixes), 3)


if __name__ == "__main__":
    unittest.main()
