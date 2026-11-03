"""The method gate of the API key users: which request calls which method, who is gated, what is refused.

Invented users and requests only. frappe is a stub in the module itself (a session user, a request path, a form).
Runs on the host, in the light gate:

    python3 -m unittest bi_finance.test_api_gate

Pinned: every path shape that reaches a whitelisted function (v1, v2, the v2 doctype route, cmd on any path) and the
two that do not (resource, document); a listed and an unlisted method per gated user; a human, Guest and api-agent
pass the unlisted one; the nine functions of erp-qg70, create_payment_proposal, search_link and submit are refused; a name
that only starts with a listed one is refused; the refusal is PermissionError (HTTP 403) and names the method alone;
the hook is auth_hooks; GATED_USERS stays inside assistant_actions.API_USERS.
The list in finance/docs/mcp.md equals ALLOWED_METHODS.
"""

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SYNC = "api-sync@finance.local"
MCP = "api-mcp@finance.local"
AGENT = "api-agent@finance.local"
HUMAN = "owner@example.com"
LISTED = "frappe.desk.query_report.get_script"
UNLISTED = "erpnext.controllers.queries.employee_query"
# erp-qg70's nine functions that answer a key with no payroll or HR role, and the ones the gate names besides
NINE = [
    "erpnextswiss.erpnextswiss.doctype.payment_proposal.payment_proposal.create_payment_proposal_for_mcp",
    "erpnext.controllers.queries.employee_query",
    "hrms.payroll.doctype.payroll_entry.payroll_entry.employee_query",
    "hrms.hr.doctype.department_approver.department_approver.get_approvers",
    "hrms.hr.doctype.appraisal.appraisal.get_kras_for_employee",
    "hrms.payroll.doctype.payroll_entry.payroll_entry.get_payroll_entries_for_jv",
    "hrms.hr.doctype.interview.interview.get_events",
    "hrms.hr.doctype.staffing_plan.staffing_plan.get_designation_counts",
    "hrms.hr.doctype.leave_application.leave_application.get_events",
]
# the others the gate names: the plain payment run, the search that calls a query by name, the ones that write
ALSO_REFUSED = [
    "frappe.desk.search.search_link",
    "frappe.desk.search.search_widget",
    "bi_finance.payment_run.create_payment_proposal",
    "erpnextswiss.erpnextswiss.doctype.payment_proposal.payment_proposal.create_payment_proposal",
    "bi_finance.assistant.approve_action",
    "frappe.client.submit",
    "frappe.client.get_list",
    "run_doc_method",
    "frappe.handler.run_doc_method",
    "hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field",
    "upload_file",
    "login",
    "logout",
]


class Frappe(types.ModuleType):
    class PermissionError(Exception):
        http_status_code = 403

    def __init__(self):
        super().__init__("frappe")
        self.session = types.SimpleNamespace(user=HUMAN)
        self.request = types.SimpleNamespace(path="/api/method/ping")
        self.form_dict = {}


with mock.patch.dict(sys.modules, {"frappe": Frappe()}):
    from bi_finance import api_gate as gate
    from bi_finance import assistant_actions as actions
    from bi_finance import hooks


class Gate(unittest.TestCase):
    def setUp(self):
        self.frappe = Frappe()
        patcher = mock.patch.object(gate, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, user, path, form=None):
        """The hook on one request: None when it passes, else the PermissionError it raised."""
        self.frappe.session.user = user
        self.frappe.request.path = path
        self.frappe.form_dict = dict(form or {})
        try:
            gate.refuse()
        except self.frappe.PermissionError as err:
            return err
        return None

    def assertPasses(self, *args, **kwargs):
        self.assertIsNone(self.call(*args, **kwargs))

    def assertRefused(self, *args, **kwargs):
        err = self.call(*args, **kwargs)
        self.assertIsInstance(err, self.frappe.PermissionError)
        return err

    # ---- the path shapes: what method_of reads

    def test_every_path_shape_that_reaches_a_whitelisted_function(self):
        shapes = {
            "/api/method/a.b.c": "a.b.c",
            "/api/v1/method/a.b.c": "a.b.c",
            "/api/v2/method/a.b.c": "a.b.c",
            "/api/method/a.b.c/": "a.b.c",
            "/api/v2/method/a.b.c/": "a.b.c",
            "/api/method/ping": "ping",
        }
        for path, method in shapes.items():
            with self.subTest(path=path):
                self.assertEqual(gate.method_of(path), method)

    def test_v1_cuts_the_name_at_the_first_slash_as_frappe_does(self):
        self.assertEqual(gate.method_of("/api/method/a.b.c/extra"), "a.b.c")
        self.assertEqual(gate.method_of("/api/v1/method/a.b.c/extra/more"), "a.b.c")

    def test_the_v2_doctype_route_is_a_name_that_cannot_be_listed(self):
        method = gate.method_of("/api/v2/method/Some Doctype/do_it")
        self.assertEqual(method, "Some Doctype/do_it")
        self.assertNotIn(method, gate.ALLOWED_METHODS)
        self.assertTrue(all("/" not in name for name in gate.ALLOWED_METHODS))

    def test_resource_and_document_routes_call_no_method(self):
        for path in (
            "/api/resource/Customer",
            "/api/resource/Customer/CUST-1",
            "/api/v1/resource/Customer",
            "/api/v2/document/Customer",
            "/api/v2/document/Customer/CUST-1/method/some_method/",
            "/api/v2/doctype/Customer/meta",
            "/app/customer",
            "/",
        ):
            with self.subTest(path=path):
                self.assertIsNone(gate.method_of(path))

    def test_cmd_names_the_method_on_any_path(self):
        """app.py checks form_dict.cmd before the path, so a cmd on a resource or a listed method path wins."""
        for path in ("/", "/api/resource/Customer", "/api/method/frappe.ping"):
            with self.subTest(path=path):
                self.assertEqual(gate.method_of(path, "a.b.c"), "a.b.c")

    def test_a_cmd_that_is_no_string_is_refused_not_passed(self):
        for cmd in (["frappe.ping"], {"a": 1}, 7, True):
            with self.subTest(cmd=cmd):
                self.assertRefused(SYNC, "/api/method/frappe.ping", {"cmd": cmd})

    def test_an_empty_cmd_does_not_hide_the_path(self):
        self.assertRefused(MCP, "/api/method/" + UNLISTED, {"cmd": ""})

    # ---- who is gated

    def test_a_listed_method_passes_for_each_gated_user(self):
        for user in (SYNC, MCP):
            for method in sorted(gate.ALLOWED_METHODS):
                with self.subTest(user=user, method=method):
                    self.assertPasses(user, "/api/method/" + method)

    def test_an_unlisted_method_is_refused_for_each_gated_user_on_every_shape(self):
        for user in (SYNC, MCP):
            for path in (
                f"/api/method/{UNLISTED}",
                f"/api/v1/method/{UNLISTED}",
                f"/api/v2/method/{UNLISTED}",
                "/api/v2/method/Employee/employee_query",
            ):
                with self.subTest(user=user, path=path):
                    self.assertRefused(user, path)

    def test_cmd_on_a_listed_path_runs_the_cmd_so_it_is_checked(self):
        self.assertRefused(SYNC, "/api/method/frappe.ping", {"cmd": UNLISTED})
        self.assertPasses(SYNC, "/api/resource/Customer", {"cmd": LISTED})

    def test_resources_and_document_routes_pass_for_gated_users(self):
        for user in (SYNC, MCP):
            for path in (
                "/api/resource/Customer",
                "/api/v2/document/Customer/CUST-1",
                "/api/v2/document/Customer/CUST-1/method/some_method/",
            ):
                with self.subTest(user=user, path=path):
                    self.assertPasses(user, path)

    def test_a_human_guest_and_api_agent_pass_the_unlisted_method(self):
        for user in (HUMAN, "Guest", "Administrator", AGENT):
            for path in (
                f"/api/method/{UNLISTED}",
                f"/api/v2/method/{UNLISTED}",
                "/api/v2/method/Employee/employee_query",
            ):
                with self.subTest(user=user, path=path):
                    self.assertPasses(user, path)
            self.assertPasses(user, "/", {"cmd": UNLISTED})

    # ---- what is refused

    def test_the_nine_functions_of_erp_qg70_are_refused_for_each_gated_user(self):
        for user in (SYNC, MCP):
            for method in NINE + ALSO_REFUSED:
                with self.subTest(user=user, method=method):
                    self.assertRefused(user, "/api/method/" + method)
                    self.assertNotIn(method, gate.ALLOWED_METHODS)

    def test_submit_is_refused_for_each_gated_user_on_every_shape(self):
        for user in (SYNC, MCP):
            for path, form in (
                ("/api/method/frappe.client.submit", None),
                ("/api/v1/method/frappe.client.submit", None),
                ("/api/v2/method/frappe.client.submit", None),
                ("/api/resource/Sales Invoice", {"cmd": "frappe.client.submit"}),
            ):
                with self.subTest(user=user, path=path):
                    self.assertRefused(user, path, form)
        self.assertNotIn("frappe.client.submit", gate.ALLOWED_METHODS)

    def test_search_link_is_refused_with_a_query_that_names_a_whitelisted_function(
        self,
    ):
        err = self.assertRefused(
            MCP,
            "/api/method/frappe.desk.search.search_link",
            {"doctype": "Employee", "query": UNLISTED, "txt": ""},
        )
        self.assertNotIn("Employee", str(err))

    def test_nothing_in_frappe_desk_search_and_no_hrms_or_erpnextswiss_method_is_listed(
        self,
    ):
        for name in gate.ALLOWED_METHODS:
            with self.subTest(name=name):
                self.assertFalse(name.startswith("frappe.desk.search"))
                self.assertFalse(
                    name.startswith(("hrms.", "erpnextswiss.", "bi_payroll."))
                )
                self.assertNotIn("run_doc_method", name)
                self.assertNotIn("create_payment_proposal", name)

    def test_a_name_that_only_starts_with_a_listed_one_is_refused(self):
        for name in sorted(gate.ALLOWED_METHODS):
            for longer in (
                name + "_x",
                name + ".x",
                name + "2",
                name + " ",
                name.upper(),
                " " + name,
            ):
                with self.subTest(longer=longer):
                    self.assertRefused(SYNC, "/api/method/" + longer)
                    self.assertRefused(MCP, "/", {"cmd": longer})
        for shorter in (
            "frappe",
            "frappe.desk",
            "frappe.desk.query_report",
            "frappe.client",
        ):
            with self.subTest(shorter=shorter):
                self.assertRefused(MCP, "/api/method/" + shorter)

    def test_the_list_holds_exact_dotted_names_only(self):
        self.assertIsInstance(gate.ALLOWED_METHODS, frozenset)
        for name in gate.ALLOWED_METHODS:
            with self.subTest(name=name):
                self.assertEqual(name, name.strip())
                self.assertNotIn("*", name)
                self.assertNotIn("/", name)

    def test_mcp_doc_lists_the_allowed_methods(self):
        doc = Path(__file__).resolve().parents[3] / "docs" / "mcp.md"
        text = doc.read_text()
        match = re.search(r"The list is short:(.*?)(?:\.(?=\s+`)|$)", text, re.DOTALL)
        self.assertIsNotNone(
            match, "Could not find the allowed-method sentence in mcp.md"
        )
        names = set(re.findall(r"`([^`]+)`", match.group(1)))
        missing = sorted(gate.ALLOWED_METHODS - names)
        extra = sorted(names - gate.ALLOWED_METHODS)
        self.assertEqual(
            names,
            set(gate.ALLOWED_METHODS),
            f"mcp.md missing allowed methods: {missing}; extra methods: {extra}",
        )

    # ---- the refusal

    def test_the_refusal_is_a_permission_error_and_answers_403(self):
        err = self.assertRefused(SYNC, "/api/method/" + UNLISTED)
        self.assertEqual(self.frappe.PermissionError.http_status_code, 403)
        self.assertEqual(err.http_status_code, 403)

    def test_the_refusal_names_the_method_and_nothing_else(self):
        err = self.assertRefused(
            MCP,
            "/api/method/" + UNLISTED,
            {
                "doctype": "Employee",
                "txt": "SECRET-TEXT",
                "filters": {"x": "SECRET-FILTER"},
            },
        )
        self.assertIn(UNLISTED, str(err))
        for word in (MCP, "SECRET-TEXT", "SECRET-FILTER", "Employee"):
            self.assertNotIn(word, str(err))

    def test_a_very_long_method_name_is_cut_in_the_refusal(self):
        err = self.assertRefused(SYNC, "/api/method/" + "a" * 5000)
        self.assertLess(len(str(err)), 400)

    # ---- the wiring

    def test_the_hook_is_auth_hooks_not_before_request(self):
        """Frappe validates a token after the before_request hooks run, so there a key user is still Guest."""
        self.assertEqual(hooks.auth_hooks, ["bi_finance.api_gate.refuse"])
        self.assertFalse(hasattr(hooks, "before_request"))

    def test_the_hook_names_a_function_of_the_module(self):
        module, function = hooks.auth_hooks[0].rsplit(".", 1)
        self.assertEqual(module, gate.__name__)
        self.assertTrue(callable(getattr(gate, function)))

    def test_the_gated_users_stay_inside_the_api_users_that_never_approve(self):
        self.assertEqual(gate.GATED_USERS, {SYNC, MCP})
        self.assertLessEqual(gate.GATED_USERS, actions.API_USERS)
        self.assertNotIn(AGENT, gate.GATED_USERS)


if __name__ == "__main__":
    unittest.main()
