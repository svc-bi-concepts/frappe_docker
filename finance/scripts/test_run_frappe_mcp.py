"""Offline tests for finance/mcp/run-frappe-mcp.sh: which key file it reads, and the one warning.

The server binary is a stub in a copy of the script's folder, so nothing starts; the keys are invented.
Run with: cd finance/scripts && python3 -m unittest test_run_frappe_mcp
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "mcp", "run-frappe-mcp.sh")

# Prints what the wrapper handed the server; the secret is checked by value, never echoed to the test output.
SERVER_STUB = """#!/bin/sh
echo "url=$FRAPPE_URL key=$FRAPPE_API_KEY secret=$FRAPPE_API_SECRET"
"""


class RunFrappeMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.yard = os.path.join(self.home, "ws_yardr_finance")
        os.makedirs(self.yard)
        folder = os.path.join(self.tmp.name, "mcp")
        os.makedirs(os.path.join(folder, "venv", "bin"))
        self.script = os.path.join(folder, "run-frappe-mcp.sh")
        shutil.copy(SCRIPT, self.script)
        server = os.path.join(folder, "venv", "bin", "frappe-mcp-server")
        with open(server, "w") as f:
            f.write(SERVER_STUB)
        os.chmod(server, 0o755)

    def keys(self, name, tag):
        with open(os.path.join(self.yard, name), "w") as f:
            f.write(
                f"url=http://127.0.0.1:8080/api/\napi_key=key-{tag}\napi_secret=secret-{tag}\n"
            )

    def run_script(self):
        return subprocess.run(
            ["sh", self.script],
            env={"PATH": os.environ["PATH"], "HOME": self.home},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_the_mcp_key_is_read_when_it_exists(self):
        self.keys(".erpnext-api-mcp", "mcp")
        self.keys(".erpnext-api", "agent")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "url=http://127.0.0.1:8080 key=key-mcp secret=secret-mcp",
        )
        self.assertEqual(proc.stderr, "")

    def test_without_the_mcp_key_it_reads_the_agent_key_and_warns_once(self):
        self.keys(".erpnext-api", "agent")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "url=http://127.0.0.1:8080 key=key-agent secret=secret-agent",
        )
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertIn("warning", proc.stderr)
        self.assertIn(".erpnext-api-mcp", proc.stderr)
        self.assertNotIn("secret-agent", proc.stderr)

    def test_no_key_file_at_all_refuses(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--user mcp", proc.stderr)


if __name__ == "__main__":
    unittest.main()
