import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

SCRIPT = Path(__file__).with_name("site-check.sh")
PYTHON = Path(__file__).with_name("site-check.py")


def load_module():
    spec = importlib.util.spec_from_file_location("site_check", PYTHON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDB:
    def sql(self, query, values=None):
        if "from `__Auth`" in query:
            return [(1,)]
        if "sum(debit)" in query:
            return [("Invented Account", Decimal("2000000.00"), Decimal("2162000.00"))]
        if "select file_url" in query:
            return []
        if "group by docstatus" in query:
            return [(1, 1)]
        return [(1,)]


class SiteCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.private.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.site_report = self.root / "site-report.txt"
        self.site_report.write_text(
            "count.GL Entry.total=1\nbalance.Invented Account=debit:2000000.00,credit:2162000.00\n"
            "files.public.present=2\nfiles.public.missing=0\nfiles.private.present=1\nfiles.private.missing=0\n"
            "secret.api_user.api_secret=ok\nsecret.wise.api_token=ok\nsecret.paypal.client_secret=ok\n"
        )
        docker = self.bin / "docker"
        docker.write_text("#!/bin/sh\n" "cat >/dev/null\n" 'cat "$FAKE_SITE_REPORT"\n')
        docker.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env['PATH']}",
            FINANCE_PRIVATE=str(self.private),
            FINANCE_PROJECT="frappe-finance-copy",
            FAKE_SITE_REPORT=str(self.site_report),
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args):
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_report_uses_stub_docker_and_restricts_permissions(self):
        result = self.run_script("report", "before")
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.private / "site-check"
        report = directory / "before.txt"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
        self.assertEqual(report.read_text(), self.site_report.read_text())
        digests = result.stdout.splitlines()
        self.assertEqual(
            [line.split("=", 1)[0] for line in digests],
            ["counts", "trial_balance", "files", "secrets"],
        )
        self.assertTrue(all(len(line.split("=", 1)[1]) == 64 for line in digests))
        self.assertNotIn("Invented Account", result.stdout)

    def test_equal_reports_pass(self):
        directory = self.private / "site-check"
        directory.mkdir()
        (directory / "a.txt").write_text(self.site_report.read_text())
        (directory / "b.txt").write_text(self.site_report.read_text())
        result = self.run_script("compare", "a", "b")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "equal"))

    def test_secret_failure_is_a_difference_even_when_both_fail(self):
        directory = self.private / "site-check"
        directory.mkdir()
        report = self.site_report.read_text().replace(
            "secret.wise.api_token=ok", "secret.wise.api_token=fails"
        )
        (directory / "a.txt").write_text(report)
        (directory / "b.txt").write_text(report)
        result = self.run_script("compare", "a", "b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("secret.wise.api_token=fails", result.stdout)

    def test_digests_compare_report_outputs(self):
        directory = self.private / "site-check"
        directory.mkdir()
        (directory / "a.txt").write_text(self.site_report.read_text())
        (directory / "b.txt").write_text(self.site_report.read_text())
        for name in ("a", "b"):
            result = self.run_script("report", name)
            self.assertEqual(result.returncode, 0, result.stderr)
            (directory / f"{name}.digests").write_text(result.stdout)
        equal = self.run_script(
            "digests", str(directory / "a.digests"), str(directory / "b.digests")
        )
        self.assertEqual((equal.returncode, equal.stdout.strip()), (0, "equal"))
        changed = (directory / "b.digests").read_text().splitlines()
        changed[0] = "counts=" + ("0" if changed[0][7] != "0" else "1") + changed[0][8:]
        (directory / "b.digests").write_text("\n".join(changed) + "\n")
        unequal = self.run_script(
            "digests", str(directory / "a.digests"), str(directory / "b.digests")
        )
        self.assertEqual(unequal.returncode, 1)
        self.assertIn("different sections: counts", unequal.stdout)

    def test_changed_account_missing_file_and_failed_secret_are_differences(self):
        cases = (
            (
                "balance.Invented Account=debit:2000000.00,credit:2162000.00",
                "balance.Invented Account=debit:2000000.00,credit:2162001.00",
            ),
            ("files.private.missing=0", "files.private.missing=1"),
            ("secret.wise.api_token=ok", "secret.wise.api_token=fails"),
        )
        directory = self.private / "site-check"
        directory.mkdir()
        (directory / "a.txt").write_text(self.site_report.read_text())
        for old, new in cases:
            with self.subTest(new=new):
                (directory / "b.txt").write_text(
                    self.site_report.read_text().replace(old, new)
                )
                result = self.run_script("compare", "a", "b")
                self.assertEqual(result.returncode, 1)
                self.assertIn("not equal", result.stdout)

    def test_backend_report_never_contains_decrypted_values(self):
        password = types.ModuleType("frappe.utils.password")
        password.get_decrypted_password = (
            lambda *args, **kwargs: "invented-secret-do-not-report"
        )
        utils = types.ModuleType("frappe.utils")
        utils.password = password
        sys.modules["frappe.utils"] = utils
        sys.modules["frappe.utils.password"] = password
        try:
            module = load_module()
            frappe = types.SimpleNamespace(
                db=FakeDB(),
                get_meta=lambda doctype: types.SimpleNamespace(
                    is_submittable=doctype
                    in {
                        "Journal Entry",
                        "Sales Invoice",
                        "Purchase Invoice",
                        "Payment Entry",
                        "Bank Transaction",
                        "Salary Slip",
                    }
                ),
                get_site_path=lambda *parts: os.path.join(self.root, *parts),
            )
            report = module.build_report(frappe)
            self.assertNotIn("invented-secret-do-not-report", report)
            self.assertIn("secret.api_user.api_secret=ok", report)
            self.assertIn(
                "balance.Invented Account=debit:2000000.00,credit:2162000.00", report
            )
        finally:
            sys.modules.pop("frappe.utils.password", None)
            sys.modules.pop("frappe.utils", None)


if __name__ == "__main__":
    unittest.main()
