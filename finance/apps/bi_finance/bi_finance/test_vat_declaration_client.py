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
        self.assertRegex(source, r'@frappe\.whitelist\(methods=\["POST"\]\)\ndef fill_payment_basis\(')

    def test_the_preview_is_a_dry_run_and_the_write_is_not(self):
        script = read(SCRIPT)
        self.assertEqual(len(re.findall(r"dry_run: 1", script)), 1)
        self.assertEqual(len(re.findall(r"dry_run: 0", script)), 1)

    def test_a_submitted_declaration_gets_a_disabled_button(self):
        self.assertIn("button.prop(\"disabled\", true)", read(SCRIPT))
        self.assertIn("frm.doc.docstatus === 1", read(SCRIPT))


if __name__ == "__main__":
    unittest.main()
