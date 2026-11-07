"""Image gate contract, exercised in a disposable bench without Docker or Frappe."""

import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "finance/scripts"


class ImageGate(unittest.TestCase):
    def run_gate(self, failing=False, empty=False):
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            scripts = bench / "finance/scripts"
            scripts.mkdir(parents=True)
            shutil.copy(SCRIPTS / "gate-tests.sh", scripts)
            # Real unittest discovery/imports with synthetic cases prove failure propagation.
            (scripts / "gate-modules.sh").write_text(
                "set --\n" if empty else "set -- test_first test_second\n"
            )
            app = bench / "apps/bi_finance"
            app.mkdir(parents=True)
            for name in ("first", "second"):
                (app / f"test_{name}.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n"
                    f"    def test_case(self): self.assertTrue({not failing or name == 'first'})\n"
                )
            python = bench / "env/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            return subprocess.run(
                ["sh", str(scripts / "gate-tests.sh")],
                cwd=tmp,
                env={"PATH": os.defpath, "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
            )

    def test_success_runs_both_modules_and_summarizes_on_stdout(self):
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Ran 2 tests", run.stderr)
        self.assertIn("gate-tests: PASS (2 modules)", run.stdout)

    def test_failure_is_nonzero_and_summarized_on_stdout(self):
        run = self.run_gate(failing=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("Ran 2 tests", run.stderr)
        self.assertIn("failures=1", run.stderr)
        self.assertIn("gate-tests: FAIL (2 modules, exit 1)", run.stdout)

    def test_empty_manifest_refuses_a_false_green(self):
        run = self.run_gate(empty=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn("empty module list", run.stderr)

    def test_manifest_is_sourceable_by_posix_shell(self):
        run = subprocess.run(
            [
                "sh",
                "-c",
                '. "$1"; printf "%s\\n" "$@"',
                "sh",
                str(SCRIPTS / "gate-modules.sh"),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        modules = run.stdout.splitlines()
        self.assertEqual(len(modules), 12)
        self.assertEqual(len(set(modules)), 12)
        self.assertTrue(all(m.startswith("bi_finance.test_") for m in modules))


class FinanceBuildGraph(unittest.TestCase):
    """Static Dockerfile graph proof; the first real consumer build runs in Coolify."""

    def test_finance_target_inherits_patched_backend_and_installs_both_apps(self):
        source = (ROOT / "images/custom/Containerfile").read_text()
        # Normalize Dockerfile instructions, excluding comments and continuations.
        lines = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        stages = {}
        current = None
        for line in lines.replace("\\\n", " ").splitlines():
            if not line.strip():
                continue
            op, _, value = line.strip().partition(" ")
            if op == "FROM":
                base, _, current = shlex.split(value)
                stages[current] = {"base": base, "instructions": []}
            elif current:
                stages[current]["instructions"].append((op, value))
        self.assertEqual(stages["builder"]["base"], "base")
        self.assertEqual(stages["backend"]["base"], "base")
        self.assertEqual(stages["finance"]["base"], "backend")
        self.assertEqual(stages["default"]["base"], "backend")
        backend = stages["backend"]["instructions"]
        self.assertTrue(
            any(op == "COPY" and "--from=builder" in value for op, value in backend)
        )
        finance = stages["finance"]["instructions"]
        copies = [shlex.split(value) for op, value in finance if op == "COPY"]
        for app in ("bi_finance", "bi_payroll"):
            dest = f"/home/frappe/frappe-bench/apps/{app}"
            self.assertIn(["--chown=frappe:0", f"finance/apps/{app}", dest], copies)
            self.assertTrue(
                any(
                    op == "RUN" and f"pip install --no-cache-dir -e {dest}" in value
                    for op, value in finance
                )
            )
        self.assertIn(
            [
                "--chown=frappe:0",
                "finance/scripts/gate-tests.sh",
                "finance/scripts/gate-modules.sh",
                "/home/frappe/frappe-bench/finance/scripts/",
            ],
            copies,
        )
        # An ARG or ENV would serialize the secret in provenance or image config.
        for stage in stages.values():
            for op, value in stage["instructions"]:
                if op in ("ARG", "ENV"):
                    self.assertNotIn("APPS_JSON", value)
        builder_runs = [
            value for op, value in stages["builder"]["instructions"] if op == "RUN"
        ]
        init = next(value for value in builder_runs if "bench init" in value)
        self.assertIn("type=secret,id=APPS_JSON", init)
        for app in ("frappe", "erpnextswiss", "hrms"):
            self.assertLess(
                init.index(f"/home/frappe/patches/{app} apps/{app}"),
                init.index("bench build --production"),
            )
        self.assertLess(init.index("check_app_pins.py"), init.index("apply-patches.sh"))


class GateCheckoutRunner(unittest.TestCase):
    def test_checkout_gate_invokes_shared_runner_without_skipping_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / ".yardr").mkdir()
            (root / "finance/scripts").mkdir(parents=True)
            shutil.copy(ROOT / ".yardr/check", root / ".yardr/check")
            (root / ".yardr/check-host.sh").write_text("exit 0\n")
            (root / "finance/scripts/gate-image.sh").write_text(
                "echo finance-gate:fixture\n"
            )
            (root / "finance/scripts/test_gate_check.py").write_text("")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            for command, body in (
                ("docker", 'printf "%s\\n" "$@" > "$HOME/calls"\n'),
                ("pre-commit", "exit 0\n"),
            ):
                stub = bin_dir / command
                stub.write_text("#!/bin/sh\n" + body)
                stub.chmod(0o755)
            (bin_dir / "python3").symlink_to(sys.executable)
            (bin_dir / "python").symlink_to(sys.executable)
            run = subprocess.run(
                ["sh", ".yardr/check"],
                cwd=root,
                env={"HOME": str(root), "PATH": str(bin_dir) + os.pathsep + os.defpath},
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            args = calls.read_text().splitlines()
            self.assertEqual(args[:2], ["run", "--rm"])
            self.assertEqual(
                args[-3:],
                [
                    "finance-gate:fixture",
                    "sh",
                    "/home/frappe/frappe-bench/finance/scripts/gate-tests.sh",
                ],
            )
            self.assertIn(
                str(root / "finance/scripts")
                + ":/home/frappe/frappe-bench/finance/scripts:ro",
                args,
            )


if __name__ == "__main__":
    unittest.main()
