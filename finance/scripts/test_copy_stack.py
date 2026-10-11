"""Offline tests for copy-stack.sh and finance-copy.yml: the guards (live project refused, missing
image refused, memory, a copy that is not running), stub docker that logs its calls, no network.
Run with: cd finance/scripts && python3 -m unittest test_copy_stack
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "copy-stack.sh")

STUB_DOCKER = """\
echo "$*" >> "$STUB_LOG"
case "$1" in
  image) exit "${STUB_IMAGE_RC:-0}" ;;
  wait) echo 0; exit 0 ;;
  run)
    if [ "${STUB_MEM:-3000000}" != "none" ]; then
      echo "MemTotal: 9999999 kB"
      echo "MemAvailable: ${STUB_MEM:-3000000} kB"
    fi
    exit 0 ;;
esac
case "$*" in
  *" ps -q backend"*) [ "${STUB_COPY_UP:-0}" = "1" ] && echo "abc123" ;;
  *" ps -aq create-site"*) echo "cs123" ;;
  *"ls -t"*) echo "/home/frappe/frappe-bench/sites/frontend/private/backups/20261011_120000-frontend-database.sql.gz" ;;
esac
exit 0
"""


class CopyStack(unittest.TestCase):
    """copy-stack.sh in a scratch folder, with stub docker first on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="copy-stack-test-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "docker.log")
        self.private = os.path.join(self.tmp, "private")
        os.makedirs(self.private)
        with open(os.path.join(self.bin, "docker"), "w") as f:
            f.write("#!/bin/sh\n" + STUB_DOCKER)
        os.chmod(os.path.join(self.bin, "docker"), 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_script(self, args, **env):
        base = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STUB_LOG": self.log,
            "FINANCE_PRIVATE": self.private,
        }
        base.update(env)
        return subprocess.run(
            ["sh", SCRIPT] + args, env=base, capture_output=True, text=True, timeout=60
        )

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()

    def test_no_command_prints_usage(self):
        r = self.run_script([])
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_unknown_command_refused(self):
        r = self.run_script(["bogus"])
        self.assertEqual(r.returncode, 2)

    def test_up_needs_a_tag(self):
        r = self.run_script(["up"])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_live_project_is_refused_for_every_command(self):
        for args in (["up", "t1"], ["refresh"], ["down"], ["remove"]):
            with self.subTest(args=args):
                r = self.run_script(args, COPY_PROJECT="frappe-finance")
                self.assertEqual(r.returncode, 1)
                self.assertIn("never runs the live stack", r.stderr)
        self.assertEqual(self.calls(), [], "a refused project must not reach docker")

    def test_missing_image_is_refused_before_anything_starts(self):
        r = self.run_script(["up", "t1"], STUB_IMAGE_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not built", r.stderr)
        self.assertEqual(self.calls(), ["image inspect frappe-finance-custom:t1"])

    def test_low_memory_is_refused_before_the_copy_starts(self):
        r = self.run_script(["up", "t1"], STUB_MEM="2000000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs 2560 MiB", r.stderr)
        self.assertFalse([c for c in self.calls() if " up " in c], "compose up must not run")
        self.assertFalse([c for c in self.calls() if c.startswith("exec")], "no backup or restore")

    def test_memory_that_cannot_be_read_is_refused(self):
        r = self.run_script(["up", "t1"], STUB_MEM="none")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not read the free memory", r.stderr)
        self.assertFalse([c for c in self.calls() if " up " in c])

    def test_refresh_needs_the_copy_running(self):
        r = self.run_script(["refresh"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not running", r.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("exec") or " exec " in c])

    def test_up_backs_up_live_restores_then_applies_safety_in_order(self):
        r = self.run_script(["up", "t1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()

        def at(fragment):
            hits = [i for i, c in enumerate(calls) if fragment in c]
            self.assertTrue(hits, "no call contains %r" % fragment)
            return hits[0]

        order = [
            "run --rm --entrypoint cat frappe-finance-custom:t1 /proc/meminfo",
            "exec frappe-finance-backend-1 bench --site frontend backup --with-files",
            "up -d --scale scheduler=0",
            "restore /home/frappe/frappe-bench/sites/frontend/private/backups/20261011_120000-frontend-database.sql.gz",
            "set-config -p pause_scheduler 1",
            "set-config -p mute_emails 1",
            "set-config host_name http://frontend:8080",
            "migrate",
            "set-maintenance-mode off",
            '"Wise Settings", "sync_enabled", 0',
            '"Website Settings", "app_name"',
            "set-admin-password",
        ]
        positions = [at(fragment) for fragment in order]
        self.assertEqual(positions, sorted(positions), "steps ran out of order")
        host_calls = [c for c in calls if "set-config host_name" in c]
        self.assertEqual(len(host_calls), 1)
        self.assertIn("set-config host_name http://frontend:8080", host_calls[0])
        self.assertNotRegex(host_calls[0], r"tailnet|8462|8815")
        self.assertTrue(all("frappe-finance-copy" in c for c in calls if " up -d " in c))
        self.assertFalse(any("-p frappe-finance " in c for c in calls), "the live project is never named")

        pw = os.path.join(self.private, ".erpnext-admin-copy")
        self.assertEqual(oct(os.stat(pw).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.join(self.private, "copy-restore")).st_mode & 0o777), "0o700")

    def test_refresh_takes_the_live_backup_only_once_the_copy_runs(self):
        r = self.run_script(["refresh"], STUB_COPY_UP="1")
        self.assertNotIn("is not running", r.stderr)
        live = [c for c in self.calls() if "frappe-finance-backend-1" in c]
        self.assertTrue(live, "the fresh backup is taken from the live backend")
        self.assertTrue(all("frappe-finance-copy" not in c for c in live))


def services_with(path, field):
    """Service name -> value of a field that sits directly under it, read from a compose file as text."""
    found, current = {}, None
    with open(path) as f:
        for line in f:
            m = re.match(r"^  ([a-z-]+):\s*$", line)
            if m:
                current = m.group(1)
                continue
            m = re.match(r"^    %s: (.*)$" % field, line)
            if m and current:
                found[current] = m.group(1).strip()
    return found


class CopyCompose(unittest.TestCase):
    def test_no_service_restarts_by_itself(self):
        live_restarts = services_with(os.path.join(ROOT, "pwd.yml"), "restart")
        copy_restarts = services_with(os.path.join(ROOT, "finance-copy.yml"), "restart")
        unless = [name for name, value in live_restarts.items() if value == "unless-stopped"]
        self.assertTrue(unless, "pwd.yml has unless-stopped services to override")
        for name in unless:
            with self.subTest(service=name):
                self.assertEqual(copy_restarts.get(name), '"no"')

    def test_frontend_is_bound_to_the_reserved_loopback_port_only(self):
        with open(os.path.join(ROOT, "finance-copy.yml")) as f:
            text = f.read()
        self.assertIn('"127.0.0.1:8815:8080"', text)
        self.assertNotIn("8081", text)
        self.assertNotIn("8462", text)

    def test_image_comes_from_the_environment(self):
        with open(os.path.join(ROOT, "finance-copy.yml")) as f:
            text = f.read()
        self.assertIn("${COPY_IMAGE:-", text)
        self.assertNotRegex(text, r"image: frappe-finance-custom:v")


if __name__ == "__main__":
    unittest.main()
