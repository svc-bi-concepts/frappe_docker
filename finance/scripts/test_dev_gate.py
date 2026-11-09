"""Exercise the dev runner with isolated CLI boundaries, no daemon or providers."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class DevGate(unittest.TestCase):
    def run_gate(self, failing=None, missing=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            checkout = root / "checkout"
            scripts = checkout / "finance/scripts"
            scripts.mkdir(parents=True)
            shutil.copy(ROOT / "finance/scripts/dev-gate.py", scripts)
            (checkout / ".yardr").mkdir()
            (checkout / ".yardr/check-host.sh").write_text(
                'printf "host-tests %s\\n" "$PWD" >> "$HOME/calls"\n'
                'exit "${HOST_STATUS:-0}"\n'
            )
            tools = root / "bin"
            tools.mkdir()
            for name, status in (
                ("docker", "FRAPPE_STATUS"),
                ("pre-commit", "LINT_STATUS"),
            ):
                if name == missing:
                    continue
                tool = tools / name
                tool.write_text(
                    "#!/bin/sh\n"
                    f'printf "{name} %s\\n" "$PWD" "$@" >> "$HOME/calls"\n'
                    + (
                        'test "$GO111MODULE" = on && test -z "${SKIP:-}" || exit 9\n'
                        if name == "pre-commit"
                        else ""
                    )
                    + f'exit "${{{status}:-0}}"\n'
                )
                tool.chmod(0o755)
            # Only our stand-ins and a non-login system shell are reachable.
            (tools / "sh").symlink_to("/bin/sh")
            env = {
                "HOME": tmp,
                "PATH": str(tools),
                "GO111MODULE": "off",
                "SKIP": "hadolint",
            }
            if failing:
                env[
                    {
                        "frappe-tests": "FRAPPE_STATUS",
                        "host-tests": "HOST_STATUS",
                        "lint": "LINT_STATUS",
                    }[failing]
                ] = "7"
            output = root / "results.json"
            output.write_text('[{"name": "stale-green", "exit_code": 0}]')
            run = subprocess.run(
                [
                    sys.executable,
                    str(scripts / "dev-gate.py"),
                    "--image",
                    "finance-gate:fixture",
                    "--results",
                    "results.json",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            return (
                run,
                json.loads(output.read_text()),
                (root / "calls").read_text(),
                str(checkout),
            )

    def test_success_runs_workflow_commands_from_checkout_and_writes_three_results(
        self,
    ):
        run, results, calls, checkout = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            [r["name"] for r in results], ["frappe-tests", "host-tests", "lint"]
        )
        self.assertTrue(all(r["exit_code"] == 0 for r in results))
        self.assertEqual(
            calls.splitlines(),
            [
                f"docker {checkout}",
                "docker run",
                "docker --rm",
                "docker --network",
                "docker none",
                "docker --entrypoint",
                "docker sh",
                "docker finance-gate:fixture",
                "docker /home/frappe/frappe-bench/finance/scripts/gate-tests.sh",
                f"host-tests {checkout}",
                f"pre-commit {checkout}",
                "pre-commit run",
                "pre-commit --color=always",
                "pre-commit --all-files",
            ],
        )
        emitted = [
            json.loads(line.removeprefix("dev-gate-result: "))
            for line in run.stdout.splitlines()
            if line.startswith("dev-gate-result: ")
        ]
        self.assertEqual(emitted, results)

    def test_each_failure_is_nonzero_without_hiding_other_results(self):
        for failing in ("frappe-tests", "host-tests", "lint"):
            with self.subTest(failing=failing):
                run, results, _, _ = self.run_gate(failing=failing)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertEqual(len(results), 3)
                for result in results:
                    expected = 7 if result["name"] == failing else 0
                    self.assertEqual(result["exit_code"], expected)
                    self.assertIn("FAIL" if expected else "PASS", result["summary"])

    def test_missing_tool_fails_and_remaining_checks_still_run(self):
        for missing, name in (("docker", "frappe-tests"), ("pre-commit", "lint")):
            with self.subTest(missing=missing):
                run, results, _, _ = self.run_gate(missing=missing)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertEqual(len(results), 3)
                self.assertEqual(
                    next(r for r in results if r["name"] == name)["exit_code"], 127
                )


if __name__ == "__main__":
    unittest.main()
