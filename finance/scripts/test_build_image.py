"""Offline tests for the two app lists and the modes of build-image.sh: no docker, no network.

Run with: cd finance/scripts && python3 -m unittest test_build_image
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "build-image.sh")


def load(name):
    with open(os.path.join(ROOT, "finance", name)) as f:
        return json.load(f)


def is_hrms(app):
    return app["url"].rstrip("/").rsplit("/", 1)[-1] == "hrms"


class AppLists(unittest.TestCase):
    def test_live_list_has_hrms_at_pinned_tag(self):
        # Payroll runs on the live site (finance/docs/hrms.md, Decision 2026-10-10).
        hrms = [app for app in load("apps.json") if is_hrms(app)]
        self.assertEqual(hrms, [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}])

    def test_copy_list_has_hrms_at_pinned_tag(self):
        hrms = [app for app in load("apps-copy.json") if is_hrms(app)]
        self.assertEqual(hrms, [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}])

    def test_copy_list_is_live_list(self):
        self.assertEqual(load("apps-copy.json"), load("apps.json"))


class BuildImageModes(unittest.TestCase):
    def test_help_lists_both_modes(self):
        out = subprocess.run(
            ["sh", SCRIPT, "--help"], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("live|copy", out)
        self.assertIn("live", out)
        self.assertIn("copy", out)

    def run_in_layout(self, files, tag="t1", mode="live", existing_images=(), env_extra=None):
        """Run the script from a scratch layout with a stub docker; return (code, stderr, docker calls)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "finance", "scripts"))
            os.makedirs(os.path.join(tmp, "bin"))
            shutil.copy(SCRIPT, os.path.join(tmp, "finance", "scripts"))
            for name, apps in files.items():
                with open(os.path.join(tmp, "finance", name), "w") as f:
                    json.dump(apps, f)
            log = os.path.join(tmp, "docker.log")
            stub = os.path.join(tmp, "bin", "docker")
            with open(stub, "w") as f:
                f.write(
                    '#!/bin/sh\necho "$@" >> "%s"\n'
                    'if [ "$1 $2" = "image inspect" ]; then\n'
                    '  case " %s " in *" $3 "*) exit 0 ;; esac\n'
                    "  exit 1\nfi\n"
                    'if [ "$1 $2" = "buildx inspect" ]; then\n'
                    '  [ -n "$DOCKER_STUB_NO_BUILDER" ] && exit 1\n'
                    "  exit 0\nfi\n"
                    'if [ "$1 $2" = "buildx build" ]; then exit 0; fi\nexit 1\n' % (log, " ".join(existing_images))
                )
            os.chmod(stub, 0o755)
            env = {"PATH": os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"]}
            env.update(env_extra or {})
            run = subprocess.run(
                ["sh", os.path.join(tmp, "finance", "scripts", "build-image.sh"), mode, tag],
                capture_output=True, text=True, env=env,
            )
            calls = ""
            if os.path.exists(log):
                with open(log) as f:
                    calls = f.read()
            return run.returncode, run.stderr, calls

    def test_live_builds_base_then_bi_finance_then_bi_payroll(self):
        # Each layer sits on the one before it; the live tag is the last layer.
        hrms = [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps.json": hrms}, tag="t1")
        self.assertEqual(code, 0, err)
        builds = [line for line in calls.splitlines() if line.startswith("buildx build ")]
        self.assertEqual(len(builds), 3, calls)
        for build in builds:
            # The capped builder only (the 2026-10-11 OOMs), and --load so the tag
            # lands in the local store for switch-image.sh.
            self.assertIn(" --builder capped ", build)
            self.assertIn(" --load ", build)
        self.assertIn("--tag frappe-finance-custom:t1-base ", builds[0])
        # Frappe is one tag for the whole image; ERPNext and HRMS declare frappe >=16.50.0,<17.0.0.
        self.assertIn("--build-arg FRAPPE_BRANCH=v16.51.0 ", builds[0])
        # The asset build's node heap cap, and the app list as a BuildKit secret.
        self.assertIn("--build-arg NODE_OPTIONS=--max-old-space-size=900 ", builds[0])
        self.assertIn("--secret id=apps_json,src=", builds[0])
        # The capped builder cannot read the local image store: each layer's BASE comes
        # from the OCI layout the build before wrote, as a named context.
        self.assertIn("--output type=oci,tar=false,dest=", builds[0])
        self.assertIn("--build-context frappe-finance-custom:t1-base=oci-layout://", builds[1])
        self.assertIn(":t1-base ", builds[1])
        self.assertIn("--output type=oci,tar=false,dest=", builds[1])
        self.assertIn("--build-context frappe-finance-custom:t1-finance=oci-layout://", builds[2])
        self.assertIn(":t1-finance ", builds[2])
        self.assertIn("--build-arg BASE=frappe-finance-custom:t1-base --tag frappe-finance-custom:t1-finance ", builds[1])
        self.assertIn("--file finance/images/bi_finance.Containerfile", builds[1])
        self.assertIn("--build-arg BASE=frappe-finance-custom:t1-finance --tag frappe-finance-custom:t1 ", builds[2])
        self.assertIn("--file finance/images/bi_payroll.Containerfile", builds[2])

    def test_node_heap_defaults_to_900_and_follows_node_heap_mb(self):
        hrms = [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps.json": hrms}, tag="t1", env_extra={"NODE_HEAP_MB": "600"})
        self.assertEqual(code, 0, err)
        builds = [line for line in calls.splitlines() if line.startswith("buildx build ")]
        self.assertIn("--build-arg NODE_OPTIONS=--max-old-space-size=600 ", builds[0])
        code, err, calls = self.run_in_layout({"apps.json": hrms}, tag="t1")
        self.assertEqual(code, 0, err)
        builds = [line for line in calls.splitlines() if line.startswith("buildx build ")]
        self.assertIn("--build-arg NODE_OPTIONS=--max-old-space-size=900 ", builds[0])

    def test_a_node_heap_that_is_not_a_number_is_refused_before_docker(self):
        hrms = [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}]
        for bad in ("1.5G", "900 --inspect", "-1", "0x10"):
            with self.subTest(bad=bad):
                code, err, calls = self.run_in_layout(
                    {"apps.json": hrms}, tag="t1", env_extra={"NODE_HEAP_MB": bad}
                )
                self.assertEqual(code, 2)
                self.assertIn("NODE_HEAP_MB must be a number", err)
                self.assertEqual(calls, "")

    def test_refuses_an_existing_finance_layer_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1-finance"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertNotIn("buildx build", calls)

    def test_copy_refuses_a_list_without_hrms(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps-copy.json": erpnext}, mode="copy")
        self.assertNotEqual(code, 0)
        self.assertIn("no hrms", err)
        self.assertNotIn("buildx build", calls)

    def test_refuses_an_existing_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertNotIn("buildx build", calls)

    def test_refuses_an_existing_base_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, _ = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1-base"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_refuses_a_missing_builder(self):
        # The default builder must never build here: IT's capped builder is missing,
        # so the script stops before any build and says to ask IT.
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", env_extra={"DOCKER_STUB_NO_BUILDER": "1"}
        )
        self.assertEqual(code, 1)
        self.assertIn("capped", err)
        self.assertIn("IT", err)
        self.assertNotIn("buildx build", calls)

    def test_builder_env_override_reaches_the_builds(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", env_extra={"BUILDER": "mine"}
        )
        self.assertEqual(code, 0, err)
        self.assertIn(" --builder mine ", calls)

    def test_wrong_arguments_are_a_usage_error(self):
        for args in ([], ["bogus", "t1"], ["live"], ["live", "t1", "x"]):
            run = subprocess.run(["sh", SCRIPT] + args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)


APPLY = os.path.join(ROOT, "finance", "scripts", "apply-patches.sh")
PATCH_DIR = os.path.join(ROOT, "finance", "patches", "erpnextswiss")
FRAPPE_PATCH_DIR = os.path.join(ROOT, "finance", "patches", "frappe")
CONTAINERFILE = os.path.join(ROOT, "images", "custom", "Containerfile")

# The lines of frappe bundler.py at v16.51.0 around get_node_env (its line 296): the context of
# the real patch, so the patch applies to this file as to the app. The body is the upstream one.
BUNDLER = """\
def check_node_executable():
	pass


def get_node_env():
	return {"NODE_OPTIONS": f"--max_old_space_size={get_safe_max_old_space_size()}"}


def get_safe_max_old_space_size():
	return 2928
"""

# The lines of erpnextswiss item_tools.py at the pin around the stray comma (its line 43):
# the context of the real patch, so the patch applies to this invented file as to the app.
ITEM_TOOLS = [
    "def get_voucher_value(voucher_code, customer):",
    '    value = frappe.db.sql("""',
    "                SELECT ...",
    "                WHERE ",
    "                    `item_code` = %(voucher)s",
    "                    AND `parent` IN (SELECT `name` FROM `tabSales Invoice` WHERE `docstatus` = 1 AND `customer` = %(customer)s);\"\"\",",
    "            ,",
    "            {",
    "                'voucher': voucher_code,",
    "                'customer': customer",
    "            },",
    "            as_dict=True)",
]

# The lines of erpnextswiss payment_proposal.py at the pin around the expense-claim query
# (its line 796): the context of the second real patch, same idea as ITEM_TOOLS.
PAYMENT_PROPOSAL = """\
    ):
        expense_claims = frappe.db.sql(
            \"\"\"SELECT `name`,
                  `employee`,
                  `total_sanctioned_amount` AS `amount`,
                  `payable_account`
                FROM `tabExpense Claim`
                WHERE `docstatus` = 1
                  AND `status` = "Unpaid"
                  AND `is_proposed` = 0
                  AND `company` = %(company)s;\"\"\",
            {"company": company},
            as_dict=True,
        )
"""
# The lines of erpnextswiss common_functions.py at the pin around get_primary_address's
# query (its line 69): the context of the third real patch, same idea as ITEM_TOOLS.
COMMON_FUNCTIONS = "\n".join([
    "        LEFT JOIN `tabCountry` ON `tabAddress`.`country` = `tabCountry`.`name`",
    "        WHERE `link_doctype` = %(type)s AND `link_name` = %(name)s",
    "        ORDER BY `tabAddress`.`is_primary_address` DESC",
    '        LIMIT 1;\"\"\".format',
    "    try:",
    "        return frappe.db.sql(sql_query, ",
    "            {",
    "                'type': target_type, ",
    "                'name': target_name",
    "            },",
    "            as_dict=True)[0]",
]) + "\n"


# The lines of erpnextswiss salary_certificate.py at the pin (5d85c45) around the three hunks of the
# patches 0002-0004: the imports (0004), Pos 11 in fetch_dynamic (0002) and get_component_dynamic (0003).
# The bodies are the upstream ones, the untouched middle is cut: git apply needs a hunk's context, not its
# line numbers. The file at the pin ends without a newline, at `return pos - neg`.
SALARY_CERTIFICATE_PATH = "erpnextswiss/erpnextswiss/doctype/salary_certificate/salary_certificate.py"
SALARY_CERTIFICATE = '''# -*- coding: utf-8 -*-
# Copyright (c) 2018-2024, libracore (https://www.libracore.com) and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document

class SalaryCertificate(Document):
    pass


    # Pos 10.1
    self.deduction_pension = get_component_dynamic(self.employee, self.start_date, self.end_date, frappe.get_doc('Salary Certificate Settings').deduction_pension)
    # Pos 10.2
    self.deduction_pension_additional = get_component_dynamic(self.employee, self.start_date, self.end_date, frappe.get_doc('Salary Certificate Settings').deduction_pension_additional)
    # Pos 11
    self.net_salary = values[0].net
    # Pos 12
    self.source_tax_deduction = get_component_dynamic(self.employee, self.start_date, self.end_date, frappe.get_doc('Salary Certificate Settings').source_tax_deduction)
    # Pos 13.1.1


def get_component_dynamic(employee, start_date, end_date, componentlist):
    
    if len(componentlist) == 0:
        return 0.0;

    expression = "";
    
    positiv = []
    negativ = []
    for item in componentlist:
        comp = frappe.get_doc("Salary Component", item.component)
        if item.is_negativ == 1:
            negativ.append(comp.name)
        else:
            positiv.append(comp.name)

    expression = "' OR `tabSalary Detail`.`salary_component` = '"

    pos = 0.0
    neg = 0.0

    if len(positiv) > 0:
        expression_pos = expression.join(positiv)

        sql_query_pos = """SELECT `tabSalary Slip`.`employee`, IFNULL(SUM(`tabSalary Detail`.`amount`), 0) AS `amount`
            FROM `tabSalary Slip` 
            INNER JOIN `tabSalary Detail` ON `tabSalary Slip`.`name` = `tabSalary Detail`.`parent`
            WHERE `tabSalary Slip`.`employee` = '{0}'
            AND `tabSalary Slip`.`posting_date` >= '{1}'
            AND `tabSalary Slip`.`posting_date` <= '{2}'
            AND `tabSalary Slip`.`docstatus` = 1
            AND (`tabSalary Detail`.`salary_component` = '{3}');""".format(employee, start_date, end_date, expression_pos)
        values = frappe.db.sql(sql_query_pos, as_dict=True)
        pos = values[0].amount
    
    if len(negativ) > 0:
        expression_neg = expression.join(negativ)

        sql_query_neg = """SELECT `tabSalary Slip`.`employee`, IFNULL(SUM(`tabSalary Detail`.`amount`), 0) AS `amount`
            FROM `tabSalary Slip` 
            INNER JOIN `tabSalary Detail` ON `tabSalary Slip`.`name` = `tabSalary Detail`.`parent`
            WHERE `tabSalary Slip`.`employee` = '{0}'
            AND `tabSalary Slip`.`posting_date` >= '{1}'
            AND `tabSalary Slip`.`posting_date` <= '{2}'
            AND `tabSalary Slip`.`docstatus` = 1
            AND (`tabSalary Detail`.`salary_component` = '{3}');""".format(employee, start_date, end_date, expression_neg)
        values = frappe.db.sql(sql_query_neg, as_dict=True)
        neg = values[0].amount
    return pos - neg'''


# The first lines of erpnextswiss print format 605.040.18n_form._11.json at the pin (5d85c45):
# line 4, the css field, is patch 0007's target line. The html and format_data fields are cut:
# git apply needs the hunk's context, not the whole file. The cut after "disabled" keeps that
# line verbatim, comma and all, so the object does not close cleanly and the css line is parsed
# on its own. Raw string: the css carries the JSON escapes (\n, \") as the two characters they
# are, and the opening brace sits on the string's first line (a raw string keeps a \\\n continuation).
FORM11_JSON_PATH = "erpnextswiss/erpnextswiss/print_format/605.040.18n_form._11/605.040.18n_form._11.json"
FORM11_JSON = r'''{
 "align_labels_right": 0,
 "creation": "2018-01-21 20:46:35.275930",
 "css": ".print-format {\n   margin-left: 0.6mm;\n   margin-right: 0mm;\n   margin-top: 0mm;\n   margin-bottom: 0mm;\n   padding: 0px;\n   font-size: 10pt;\n   font-face: Arial, Helvetica, sans-serif;\n}\n\n/* use to debug\n.ff {\n  background-color: rgba(255, 0, 0, 0.2) !important;\n}\n*/\n\n.form11 {\n   background-image: url(\"/assets/erpnextswiss/images/605_040_18N_Form_11.png\");\n   background-size:cover;\n   width: 980px;\n   height: 1400px;\n}",
 "custom_format": 1,
 "disable_smart_shrinking": 0,
 "disabled": 0,
}'''


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


class PatchStep(unittest.TestCase):
    """apply-patches.sh, the image build's patch step, on invented trees: no docker, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.app = os.path.join(self.tmp, "app")
        self.patches = os.path.join(self.tmp, "patches")
        os.makedirs(self.app)
        os.makedirs(self.patches)
        git(self.app, "init", "-q")

    def commit(self, name, text):
        """Write a file into the app and commit it: the app at its pin."""
        os.makedirs(os.path.dirname(os.path.join(self.app, name)), exist_ok=True)
        with open(os.path.join(self.app, name), "w") as f:
            f.write(text)
        git(self.app, "add", name)
        git(self.app, "commit", "-q", "-m", "pin")

    def commit_pin_sources(self):
        """Every file of erpnextswiss the patches touch, as they read at the pin 5d85c45:
        the step applies the whole directory in one run, so the tree holds all of them."""
        self.commit("erpnextswiss/scripts/item_tools.py", "\n".join(ITEM_TOOLS) + "\n")
        self.commit(SALARY_CERTIFICATE_PATH, SALARY_CERTIFICATE)
        self.commit("erpnextswiss/erpnextswiss/doctype/payment_proposal/payment_proposal.py", PAYMENT_PROPOSAL)
        self.commit("erpnextswiss/erpnextswiss/common_functions.py", COMMON_FUNCTIONS)
        self.commit(FORM11_JSON_PATH, FORM11_JSON)

    def patch(self, name, text):
        with open(os.path.join(self.patches, name), "w") as f:
            f.write(text)

    def apply(self, patch_dir=None):
        return subprocess.run(
            ["sh", APPLY, patch_dir or self.patches, self.app],
            capture_output=True, text=True,
        )

    def read(self, name):
        with open(os.path.join(self.app, name)) as f:
            return f.read()

    def test_applies_in_name_order(self):
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-value.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 3\n OTHER = 2\n")
        # Applies only after 0001: its context reads VALUE = 3.
        self.patch("0002-value-again.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 3\n+VALUE = 5\n OTHER = 2\n")
        run = self.apply()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 5\nOTHER = 2\n")
        self.assertIn("applied: " + os.path.join(self.patches, "0002-value-again.patch"), run.stdout)

    def test_a_patch_that_does_not_apply_stops_the_step_and_names_it(self):
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-broken.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 9\n+VALUE = 3\n OTHER = 2\n")
        run = self.apply()
        self.assertEqual(run.returncode, 1)
        self.assertIn("patch does not apply to " + self.app + ": " + os.path.join(self.patches, "0001-broken.patch"), run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 1\nOTHER = 2\n")

    def test_relative_paths_work(self):
        # Regression: a relative patch path was read from the app's directory, not the caller's.
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-value.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 3\n OTHER = 2\n")
        run = subprocess.run(
            ["sh", APPLY, os.path.relpath(self.patches, self.tmp), os.path.relpath(self.app, self.tmp)],
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 3\nOTHER = 2\n")

    def test_no_patches_is_a_no_op(self):
        self.commit("tool.py", "VALUE = 1\n")
        run = self.apply()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 1\n")

    def test_wrong_arguments_are_a_usage_error(self):
        run = subprocess.run(["sh", APPLY, self.patches], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)

    def test_our_item_tools_patch_fixes_the_stray_comma(self):
        # Regression: at the pin, item_tools.py does not compile (line 43, a bare comma).
        source = "\n".join(ITEM_TOOLS) + "\n"
        with self.assertRaises(SyntaxError):
            compile(source, "item_tools.py", "exec")
        # The patches name their files from the repository root; the step applies every
        # patch of the directory, so the tree holds every file they touch.
        self.commit_pin_sources()
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        compile(self.read("erpnextswiss/scripts/item_tools.py"), "item_tools.py", "exec")

    def test_our_expense_claim_patch_pays_the_grand_total(self):
        # Regression: at the pin, the proposal's expense-claim rows carry the sanctioned amount,
        # so a claim with a VAT row would be paid short by the VAT. The patch reads grand_total.
        self.commit_pin_sources()
        path = "erpnextswiss/erpnextswiss/doctype/payment_proposal/payment_proposal.py"
        self.assertIn("`total_sanctioned_amount` AS `amount`", PAYMENT_PROPOSAL)
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        patched = self.read(path)
        self.assertIn("`grand_total` AS `amount`", patched)
        self.assertNotIn("`total_sanctioned_amount` AS `amount`", patched)

    def test_our_primary_address_patch_calls_no_format_method(self):
        # Regression: at the pin, get_primary_address's SQL string ends with `.format`, a
        # reference to the method instead of a call. The bound method went into frappe.db.sql
        # as the query and the bare except swallowed the error: the function always returned
        # None, and every Payment Proposal was refused ("Company address missing").
        self.commit_pin_sources()
        path = "erpnextswiss/erpnextswiss/common_functions.py"
        self.assertIn('LIMIT 1;""".format', COMMON_FUNCTIONS)
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        patched = self.read(path)
        self.assertIn('LIMIT 1;"""\n', patched)
        self.assertNotIn(".format\n", patched)

    def test_our_salary_certificate_patches_fix_net_sql_and_import(self):
        # At the pin, fetch_values msgprints through an unimported _, fetch_dynamic takes the slips'
        # net_pay as the Nettolohn (Ziffer 11 is Ziffer 8 less the total of 9 and 10), and
        # get_component_dynamic formats the component names straight into the SQL.
        source = SALARY_CERTIFICATE
        self.assertNotIn("from frappe import", source)
        self.assertIn("self.net_salary = values[0].net", source)
        self.assertIn(".format(employee, start_date, end_date, expression_pos)", source)
        self.commit_pin_sources()
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        patched = self.read(SALARY_CERTIFICATE_PATH)
        compile(patched, SALARY_CERTIFICATE_PATH, "exec")
        self.assertIn("from frappe import _", patched)
        self.assertIn("self.net_salary = (self.gross_salary - self.deduction_ahv", patched)
        self.assertNotIn("self.net_salary = values[0].net", patched)
        self.assertIn("IN %s", patched)
        self.assertIn("tuple(positiv)", patched)
        self.assertNotIn(".format(employee, start_date, end_date", patched)

    def test_our_form11_patch_fits_the_canvas_to_a4(self):
        # At the pin the Form 11 canvas is 980px wide. frappe's document PDF path
        # (download_pdf -> get_pdf with its default smart_shrinking=False) adds
        # --disable-smart-shrinking for wkhtmltopdf > 0.12.3, so the right-hand value column
        # fell off the printable area and the form spilled to two pages. Patch 0007 zooms
        # the canvas to the A4 printable width: 210 mm less the 15 mm page margins on both
        # sides = 180 mm = 680 px at 96 dpi; 680/980 = 0.694, so the zoom is 0.69.
        self.assertNotIn("zoom:", FORM11_JSON)
        self.assertIn("width: 980px", FORM11_JSON)
        self.commit_pin_sources()
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        css_line = next(l for l in self.read(FORM11_JSON_PATH).splitlines()
                        if l.startswith(' "css": '))
        css = json.loads("{" + css_line.rstrip(",") + "}")["css"]
        self.assertIn(".form11 {\n   zoom: 0.69;\n", css)
        self.assertIn("width: 980px", css)

    def test_our_bundler_patch_keeps_the_node_options_the_build_was_given(self):
        # Regression: frappe's bundler replaced NODE_OPTIONS with 75% of the RAM it sees, so the heap
        # cap of build-image.sh never reached the vite build and the capped builder SIGKILLed it.
        path = "frappe/bundler.py"
        self.commit(path, BUNDLER)
        run = subprocess.run(["sh", APPLY, FRAPPE_PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        scope = {"os": os}
        exec(self.read(path), scope)
        with mock.patch.dict(os.environ, {"NODE_OPTIONS": "--max-old-space-size=900"}):
            self.assertEqual(scope["get_node_env"](), {"NODE_OPTIONS": "--max-old-space-size=900"})
        with mock.patch.dict(os.environ):
            os.environ.pop("NODE_OPTIONS", None)
            self.assertEqual(scope["get_node_env"](), {"NODE_OPTIONS": "--max_old_space_size=2928"})

    def test_the_containerfile_builds_assets_after_the_frappe_patch(self):
        with open(CONTAINERFILE) as f:
            text = f.read()
        self.assertIn("--skip-assets", text)
        patched = text.index("apply-patches.sh /home/frappe/patches/frappe apps/frappe")
        self.assertLess(patched, text.index("bench build --production"))


if __name__ == "__main__":
    unittest.main()
