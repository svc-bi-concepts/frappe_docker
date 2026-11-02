"""Offline tests for public-check.sh: a stub curl answers by case (invented hosts under example.test and
documentation addresses 192.0.2.x), no network.
Run with: cd finance/scripts && python3 -m unittest test_public_check
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "public-check.sh")

# The stub reads STUB_HIT: "<scheme> <needle>" makes the request whose arguments hold the needle answer as
# Frappe (needle: a listed name, unknown-name, "Host:" for the empty Host, or the bare address URL); a request
# on STUB_REFUSE (a scheme, or "all") exits 7 like a refused connection; STUB_RC is any other exit code.
# STUB_MODE=header: every answer carries only a Frappe header; STUB_MODE=login: only /login answers as Frappe.
# One stub file serves every test: a fresh executable costs seconds to its first run on the mini.
STUB_CURL = """\
args="$*"
echo "$args" >> "$STUB_LOG"
case "$args" in *"https://"*) scheme=https ;; *) scheme=http ;; esac
[ "${STUB_RC:-}" ] && exit "$STUB_RC"
case "${STUB_MODE:-}" in
header) printf 'HTTP/1.1 302 Found\\r\\nX-Frappe-Site-Name: frontend\\r\\n\\r\\n'; exit 0 ;;
login) case "$args" in *login*) printf 'HTTP/1.1 200 OK\\r\\n\\r\\n<script>frappe.ready()</script>' ;; esac; exit 0 ;;
esac
if [ "${STUB_REFUSE:-}" = all ] || [ "${STUB_REFUSE:-}" = "$scheme" ]; then exit 7; fi
if [ -n "${STUB_HIT:-}" ]; then
  want_scheme=${STUB_HIT%% *}
  needle=${STUB_HIT#* }
  if [ "$want_scheme" = "$scheme" ]; then
    case "$args" in *"$needle"*)
      case "$args" in
      *ping*) printf 'HTTP/1.1 200 OK\\r\\n\\r\\n{"message":"pong"}' ;;
      *) printf 'HTTP/1.1 200 OK\\r\\n\\r\\n<html>Login</html>' ;;
      esac
      exit 0 ;;
    esac
  fi
fi
printf 'HTTP/1.1 404 Not Found\\r\\nServer: nginx\\r\\n\\r\\nnot found'
"""


class PublicCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin = tempfile.mkdtemp(prefix="public-check-bin-")
        with open(os.path.join(cls.bin, "curl"), "w") as f:
            f.write("#!/bin/sh\n" + STUB_CURL)
        os.chmod(os.path.join(cls.bin, "curl"), 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="public-check-test-")
        self.log = os.path.join(self.tmp, "curl.log")
        self.hosts = os.path.join(self.tmp, "public-check-hosts")
        self.write_hosts(
            "# invented\n192.0.2.10 ledger.example.test other.example.test\n"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_hosts(self, text, mode=0o600):
        with open(self.hosts, "w") as f:
            f.write(text)
        os.chmod(self.hosts, mode)

    def run_check(self, **env):
        base = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STUB_LOG": self.log,
            "PUBLIC_CHECK_HOSTS": self.hosts,
        }
        base.update(env)
        return subprocess.run(
            ["sh", SCRIPT], env=base, capture_output=True, text=True, timeout=60
        )

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_clean_run_passes_and_asks_every_case(self):
        run = self.run_check()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        calls = self.calls()
        # 2 listed names + unknown + empty Host + bare address = 5 cases, 2 schemes, 2 paths
        self.assertEqual(len(calls), 5 * 2 * 2)
        for scheme in ("http", "https"):
            port = "80" if scheme == "http" else "443"
            for name in (
                "ledger.example.test",
                "other.example.test",
                "unknown-name.invalid",
            ):
                self.assertTrue(
                    any(
                        f"--resolve {name}:{port}:192.0.2.10 {scheme}://{name}/api/method/ping"
                        in c
                        for c in calls
                    ),
                    (scheme, name),
                )
            self.assertTrue(
                any(f"-H Host: {scheme}://192.0.2.10/login" in c for c in calls), scheme
            )
            self.assertTrue(
                any(
                    c.endswith(f" {scheme}://192.0.2.10/api/method/ping")
                    and "-H" not in c
                    and "--resolve" not in c
                    for c in calls
                ),
                scheme,
            )
        self.assertIn("0 Frappe", run.stdout)

    def test_refused_host_passes(self):
        run = self.run_check(STUB_REFUSE="all")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("20 refused, 0 answered", run.stdout)

    def test_a_hit_on_each_case_fails_and_names_the_case_only(self):
        cases = {
            "ledger.example.test": "address 1, public name 1",
            "other.example.test": "address 1, public name 2",
            "unknown-name": "address 1, unknown name",
            "Host:": "address 1, empty Host",
            "-m 10 SCHEME://": "address 1, bare address",
        }
        for scheme in ("http", "https"):
            for needle, label in cases.items():
                with self.subTest(scheme=scheme, case=label):
                    run = self.run_check(
                        STUB_HIT=f"{scheme} {needle.replace('SCHEME', scheme)}"
                    )
                    self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                    self.assertIn(f"HIT: {label}, {scheme}", run.stdout)
                    # the output never carries a host or address
                    self.assertNotIn("example.test", run.stdout + run.stderr)
                    self.assertNotIn("192.0.2.10", run.stdout + run.stderr)

    def test_a_login_page_alone_is_a_hit(self):
        run = self.run_check(STUB_MODE="login")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("/login", run.stdout)
        self.assertNotIn("/api/method/ping", run.stdout)

    def test_a_frappe_header_alone_is_a_hit(self):
        run = self.run_check(STUB_MODE="header")
        self.assertEqual(run.returncode, 1, run.stdout)

    def test_one_scheme_refused_still_checks_the_other(self):
        run = self.run_check(STUB_REFUSE="http", STUB_HIT="https ledger.example.test")
        self.assertEqual(run.returncode, 1)
        self.assertIn("https", run.stdout)

    def test_hosts_file_missing_wrong_mode_or_empty_is_refused(self):
        os.remove(self.hosts)
        self.assertEqual(self.run_check().returncode, 2)
        self.write_hosts("192.0.2.10 ledger.example.test\n", mode=0o644)
        run = self.run_check()
        self.assertEqual(run.returncode, 2)
        self.assertIn("mode 600", run.stderr)
        self.write_hosts("# nothing\n\n")
        run = self.run_check()
        self.assertEqual(run.returncode, 2)
        self.assertIn("no address", run.stderr)

    def test_an_address_that_does_not_resolve_is_an_error_not_a_pass(self):
        run = self.run_check(STUB_RC="6")
        self.assertEqual(run.returncode, 2, run.stdout)

    def test_two_addresses_are_numbered(self):
        self.write_hosts("192.0.2.10 ledger.example.test\n192.0.2.11\n")
        run = self.run_check(STUB_HIT="http 192.0.2.11")
        self.assertEqual(run.returncode, 1)
        self.assertIn("address 2", run.stdout)
        self.assertNotIn("address 1", run.stdout)


if __name__ == "__main__":
    unittest.main()
