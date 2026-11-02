import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
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
    auth_rows = 1
    file_rows = ()
    folder_rows = ()

    def sql(self, query, values=None):
        if "from `__Auth`" in query:
            return [(self.auth_rows,)]
        if "sum(debit)" in query:
            return [("Invented Account", Decimal("2000000.00"), Decimal("2162000.00"))]
        if "select file_url" in query:
            rows = list(self.file_rows)
            if "is_folder = 0" not in query:
                rows += self.folder_rows
            limit, offset = values
            return rows[offset : offset + limit]
        if "group by docstatus" in query:
            return [(1, 1)]
        return [(1,)]


SUBMITTABLE = {
    "Journal Entry",
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Bank Transaction",
    "Salary Slip",
}


def fake_frappe(root, db, conf=None):
    return types.SimpleNamespace(
        db=db,
        local=types.SimpleNamespace(
            conf={"encryption_key": "invented-key"} if conf is None else conf
        ),
        get_meta=lambda doctype: types.SimpleNamespace(
            is_submittable=doctype in SUBMITTABLE
        ),
        get_site_path=lambda *parts: os.path.join(root, *parts),
    )


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
        self.docker_args = self.root / "docker-args.txt"
        self.docker_stdin = self.root / "docker-stdin.txt"
        docker.write_text(
            "#!/bin/sh\n"
            'echo "$*" >"$FAKE_DOCKER_ARGS"\n'
            'cat >"$FAKE_DOCKER_STDIN"\n'
            'if [ -n "${FAKE_DOCKER_FAIL:-}" ]; then echo partial; exit 7; fi\n'
            'cat "$FAKE_SITE_REPORT"\n'
        )
        docker.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env['PATH']}",
            FINANCE_PRIVATE=str(self.private),
            FINANCE_PROJECT="frappe-finance-copy",
            FAKE_SITE_REPORT=str(self.site_report),
            FAKE_DOCKER_ARGS=str(self.docker_args),
            FAKE_DOCKER_STDIN=str(self.docker_stdin),
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
            report = module.build_report(fake_frappe(self.root, FakeDB()))
            self.assertNotIn("invented-secret-do-not-report", report)
            self.assertIn("secret.api_user.api_secret=ok", report)
            self.assertIn(
                "balance.Invented Account=debit:2000000.00,credit:2162000.00", report
            )
        finally:
            sys.modules.pop("frappe.utils.password", None)
            sys.modules.pop("frappe.utils", None)

    def test_report_pipes_the_script_into_the_chosen_backend(self):
        self.assertEqual(self.run_script("report", "copy").returncode, 0)
        args = self.docker_args.read_text()
        self.assertIn("compose -p frappe-finance-copy ", args)
        self.assertIn(" exec -T backend ", args)
        self.assertEqual(self.docker_stdin.read_text(), PYTHON.read_text())
        del self.env["FINANCE_PROJECT"]
        self.assertEqual(self.run_script("report", "live").returncode, 0)
        self.assertIn("compose -p frappe-finance ", self.docker_args.read_text())

    def test_other_project_is_refused_before_docker(self):
        self.env["FINANCE_PROJECT"] = "another-stack"
        result = self.run_script("report", "a")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.docker_args.exists())

    def test_failed_backend_keeps_the_old_report_and_prints_nothing(self):
        self.assertEqual(self.run_script("report", "a").returncode, 0)
        report = self.private / "site-check" / "a.txt"
        before = report.read_text()
        self.env["FAKE_DOCKER_FAIL"] = "1"
        result = self.run_script("report", "a")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")
        self.assertEqual(report.read_text(), before)
        self.assertEqual(os.listdir(report.parent), ["a.txt"])

    def test_report_names_cannot_leave_the_directory(self):
        for name in ("../a", "a/b", "..", ""):
            with self.subTest(name=name):
                self.assertEqual(self.run_script("report", name).returncode, 2)
                self.assertEqual(self.run_script("compare", name, "b").returncode, 2)

    def test_unreadable_input_is_an_error_and_no_verdict(self):
        directory = self.private / "site-check"
        directory.mkdir()
        (directory / "a.txt").write_text(self.site_report.read_text())
        missing = self.run_script("compare", "a", "absent")
        self.assertEqual((missing.returncode, missing.stdout), (2, ""))
        self.assertNotIn("Traceback", missing.stderr)
        absent = self.run_script("digests", str(directory / "x"), str(directory / "y"))
        self.assertEqual((absent.returncode, absent.stdout), (2, ""))

    def test_digests_refuse_output_that_is_no_digest_output(self):
        directory = self.private / "site-check"
        directory.mkdir()
        self.assertEqual(self.run_script("report", "a").returncode, 0)
        good = self.run_script("report", "a").stdout
        cases = {
            "empty": "",
            "section missing": "\n".join(good.splitlines()[1:]) + "\n",
            "records": good + "balance.Invented Account=1\n",
            "not hex": good.replace(good[7:71], "z" * 64, 1),
        }
        (directory / "good").write_text(good)
        for label, text in cases.items():
            with self.subTest(label):
                (directory / "bad").write_text(text)
                result = self.run_script(
                    "digests", str(directory / "good"), str(directory / "bad")
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid digest output", result.stdout)

    def test_a_change_moves_only_its_own_section_digest(self):
        first = self.run_script("report", "a").stdout.splitlines()
        self.site_report.write_text(
            self.site_report.read_text().replace(
                "files.private.missing=0", "files.private.missing=1"
            )
        )
        second = self.run_script("report", "b").stdout.splitlines()
        changed = [x.split("=")[0] for x, y in zip(first, second) if x != y]
        self.assertEqual(changed, ["files"])


class BackendTests(unittest.TestCase):
    """build_report as it runs in the backend, with frappe stubbed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.decrypt = lambda: "invented-secret-do-not-report"
        password = types.ModuleType("frappe.utils.password")

        def get_decrypted_password(*args, **kwargs):
            self.calls.append(args)
            return self.decrypt()

        password.get_decrypted_password = get_decrypted_password
        modules = {"frappe.utils": types.ModuleType("frappe.utils")}
        modules["frappe.utils.password"] = password
        patcher = unittest.mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = load_module()

    def touch(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    def file_lines(self, db):
        report = self.module.build_report(fake_frappe(self.root, db))
        return [line for line in report.splitlines() if line.startswith("files.")]

    def test_secret_names_are_the_ones_the_code_uses(self):
        self.module.build_report(fake_frappe(self.root, FakeDB()))
        self.assertEqual(
            sorted(self.calls),
            [
                ("PayPal Settings", "PayPal Settings", "client_secret"),
                ("User", "api-agent@finance.local", "api_secret"),
                ("Wise Settings", "Wise Settings", "api_token"),
            ],
        )

    def test_a_secret_that_does_not_decrypt_fails(self):
        def invalid():
            raise ValueError("invented-secret-do-not-report")

        for label, decrypt in (("none", lambda: None), ("raises", invalid)):
            with self.subTest(label):
                self.decrypt = decrypt
                report = self.module.build_report(fake_frappe(self.root, FakeDB()))
                self.assertIn("secret.wise.api_token=fails", report)
                self.assertNotIn("invented-secret-do-not-report", report)

    def test_a_secret_without_a_row_is_not_set_and_not_decrypted(self):
        db = FakeDB()
        db.auth_rows = 0
        report = self.module.build_report(fake_frappe(self.root, db))
        self.assertIn("secret.paypal.client_secret=not set", report)
        self.assertEqual(self.calls, [])

    def test_a_site_without_a_key_fails_and_is_not_given_one(self):
        # Frappe's decrypt would write a new key to site_config.json here.
        report = self.module.build_report(fake_frappe(self.root, FakeDB(), conf={}))
        self.assertIn("secret.api_user.api_secret=fails", report)
        self.assertEqual(self.calls, [])

    def test_file_rows_against_the_disk(self):
        self.touch("public", "files", "here.pdf")
        self.touch("private", "files", "here.pdf")
        self.touch("private", "files", "named-only.pdf")
        self.touch("secret.txt")
        db = FakeDB()
        db.file_rows = [
            ("/files/here.pdf", "here.pdf", 0),
            ("/files/gone.pdf", "gone.pdf", 0),
            ("/private/files/here.pdf", "here.pdf", 1),
            ("/private/files/gone.pdf", "gone.pdf", 1),
            (None, "named-only.pdf", 1),
            ("/files/../../secret.txt", "secret.txt", 0),
            ("https://invented.example/remote.pdf", "remote.pdf", 0),
            ("/api/method/invented.download", "generated.pdf", 1),
        ]
        db.folder_rows = [(None, "Home", 0), (None, "Attachments", 0)]
        self.assertEqual(
            self.file_lines(db),
            [
                "files.private.missing=1",
                "files.private.present=2",
                "files.public.missing=2",
                "files.public.present=1",
            ],
        )

    def test_the_url_says_where_a_file_lies(self):
        self.touch("public", "files", "moved.pdf")
        db = FakeDB()
        db.file_rows = [("/files/moved.pdf", "moved.pdf", 1)]
        self.assertIn("files.private.present=1", self.file_lines(db))

    def test_file_rows_are_read_in_batches(self):
        self.module.FILE_BATCH = 2
        self.touch("public", "files", "here.pdf")
        db = FakeDB()
        db.file_rows = [("/files/here.pdf", "here.pdf", 0)] * 5
        self.assertIn("files.public.present=5", self.file_lines(db))


if __name__ == "__main__":
    unittest.main()
