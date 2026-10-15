"""The VAT Declaration's Desk script is registered and calls the method the server offers, stdlib only.

No JS runner is in this app, so the dialog and the buttons are checked on the HRMS copy site by hand (see
finance/docs/erpnext-setup.md). This test holds the wiring: hooks.py names the file, the file exists in the app's
public folder, and the method it calls is whitelisted for POST in vat_declaration.py.

    python3 -m unittest bi_finance.test_vat_declaration_client
"""

import os
import re
import unittest

import bi_finance.hooks as hooks

PACKAGE = os.path.dirname(os.path.abspath(hooks.__file__))
SCRIPT = hooks.doctype_js["VAT Declaration"]
METHOD = "bi_finance.vat_declaration.fill_payment_basis"


def read(relative):
    with open(os.path.join(PACKAGE, relative), encoding="utf-8") as f:
        return f.read()


class Registration(unittest.TestCase):
    def test_hooks_register_the_script_on_the_vat_declaration(self):
        self.assertEqual(SCRIPT, "public/js/vat_declaration.js")

    def test_the_registered_script_exists_in_public(self):
        self.assertTrue(os.path.isfile(os.path.join(PACKAGE, SCRIPT)), SCRIPT)

    def test_the_script_registers_on_the_same_doctype(self):
        self.assertIn("frappe.ui.form.on('VAT Declaration'", read(SCRIPT))


class Method(unittest.TestCase):
    def test_the_script_calls_a_whitelisted_method_for_post(self):
        self.assertIn('"%s"' % METHOD, read(SCRIPT))
        source = read("vat_declaration.py")
        self.assertRegex(
            source,
            r'@frappe\.whitelist\(methods=\["POST"\]\)\ndef fill_payment_basis\(',
        )

    def test_the_preview_is_a_dry_run_and_the_write_is_not(self):
        script = read(SCRIPT)
        self.assertEqual(len(re.findall(r"dry_run: 1", script)), 1)
        self.assertEqual(len(re.findall(r"dry_run: 0", script)), 1)

    def test_a_submitted_declaration_gets_a_disabled_button(self):
        self.assertIn('button.prop("disabled", true)', read(SCRIPT))
        self.assertIn("frm.doc.docstatus === 1", read(SCRIPT))

    def test_the_fill_button_shows_on_a_new_form_once_company_and_dates_are_set(self):
        script = read(SCRIPT)
        self.assertIn(
            "frm.doc.company && frm.doc.start_date && frm.doc.end_date", script
        )
        self.assertNotIn("if (!frm.is_new()) {", script)

    def test_a_write_routes_to_the_draft_the_fill_names(self):
        script = read(SCRIPT)
        self.assertIn(
            'frappe.set_route("Form", "VAT Declaration", r.message.name)', script
        )
        self.assertNotIn("frm.reload_doc()", script)

    def test_the_fill_returns_the_draft_name_with_the_fields(self):
        source = read("vat_declaration.py")
        self.assertIn('return {"name": doc.name, "fields": shown}', source)
        self.assertIn('return {"fields": shown}', source)


class Desk(unittest.TestCase):
    def test_get_values_is_removed_on_a_draft_after_erpnextswiss_adds_it(self):
        script = read(SCRIPT)
        self.assertIn('frm.remove_custom_button(__("Get values"))', script)
        self.assertLess(
            script.index("docstatus === 0"),
            script.index('frm.remove_custom_button(__("Get values"))'),
        )

    def test_the_intro_names_the_fill_and_the_recalculation(self):
        self.assertIn(
            "Fill from payments fills this draft; Recalculate updates 289/299.",
            read(SCRIPT),
        )

    def test_the_title_of_a_new_declaration_follows_the_period_convention(self):
        script = read(SCRIPT)
        self.assertIn('"MWST " + period', script)
        self.assertIn("'start_date': function(frm) { set_title(frm) }", script)
        self.assertIn("'end_date': function(frm) { set_title(frm) }", script)

    def test_the_title_rule_replaces_erpnextswiss_default_and_our_own_only(self):
        script = read(SCRIPT)
        self.assertIn(r"var DEFAULT_TITLE = /^Q[1-4] \/ \d{4}( - .*)?$/;", script)
        self.assertIn(r"var PERIOD_TITLE = /^MWST \d{4}(Q[1-4]|H[12])$/;", script)
        self.assertIn("!DEFAULT_TITLE.test(title) && !PERIOD_TITLE.test(title)", script)

    def test_the_title_is_set_on_refresh_as_well_as_on_the_dates(self):
        # erpnextswiss's onload sets its default title after the dates, so the refresh has the last word on a new form
        script = read(SCRIPT)
        self.assertIn("if (frm.is_new()) {\n            set_title(frm);", script)


if __name__ == "__main__":
    unittest.main()
