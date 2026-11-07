"""Observe gate module arguments; the image oracle is the original gate contract."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE_MODULES = {
    "bi_finance.test_payment_run",
    "bi_finance.test_treasury",
    "bi_finance.test_bank_list",
    "bi_finance.test_bank_statement_upload",
    "bi_finance.test_cash_flow_forecast",
    "bi_finance.test_cash_position",
    "bi_finance.test_paypal",
    "bi_finance.test_paypal_sync",
    "bi_finance.test_qrbill",
    "bi_finance.test_setup",
    "bi_finance.test_wise",
    "bi_finance.test_hrms_accrual_filter",
}


class GateModules(unittest.TestCase):
    def test_executed_modules_partition_all_finance_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "bench"
            root.mkdir()
            scripts = root / "finance/scripts"
            scripts.mkdir(parents=True)
            shutil.copytree(ROOT / ".yardr", root / ".yardr")
            for name in ("gate-tests.sh", "gate-modules.sh"):
                shutil.copy(ROOT / "finance/scripts" / name, scripts)
            for path in ("finance/apps/bi_finance", "finance/apps/bi_payroll", "finance/bexio", "finance/sharepoint", "finance/mail", "finance/wise", "apps/bi_finance", "env/bin", "bin"):
                (root / path).mkdir(parents=True)
            stub = root / "bin/python3"
            stub.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "if sys.argv[1:3] == ['-m', 'unittest']:\n"
                "    with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[3:]) + '\\n')\n"
            )
            stub.chmod(0o755)
            (root / "env/bin/python").symlink_to(stub)
            for name in ("docker", "git"):
                command = root / "bin" / name
                command.write_text(
                    '#!/bin/sh\n'
                    'case "$*" in *finance-coolify.yml*)\n'
                    '  [ -n "${LEDGER_HOST:-}" ] && [ -n "${DB_ROOT_PASSWORD:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ] || exit 1;;\n'
                    'esac\nprintf "{}\\n"\n'
                    if name == "docker" else "#!/bin/sh\nexit 0\n"
                )
                command.chmod(0o755)
            env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.defpath, TMPDIR=tmp, CALLS=str(root / "calls"))
            observed = []
            for command in (".yardr/check-host.sh", "finance/scripts/gate-tests.sh"):
                run = subprocess.run(["sh", command], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                observed.append([arg for call in calls for arg in call if arg.startswith("bi_finance.")])
                (root / "calls").unlink()
            host, image = observed
            self.assertCountEqual(image, IMAGE_MODULES)
            on_disk = {"bi_finance." + p.stem for p in (ROOT / "finance/apps/bi_finance/bi_finance").glob("test_*.py")}
            self.assertCountEqual(host + image, on_disk)


if __name__ == "__main__":
    unittest.main()
