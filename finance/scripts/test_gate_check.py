"""Offline tests for the checkout guard at the top of .yardr/check: a checkout outside $HOME stops
the gate before any docker call (Colima mounts only the home folder). Only the guard runs in the
'outside' case; the host lines behind it are not checked here. Invented folders under a temp dir,
stub docker that records its calls, no network. Run with: cd finance/scripts && python3 -m unittest test_gate_check
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHECK = os.path.join(ROOT, ".yardr", "check")


class GateCheckGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        self.marker = os.path.join(self.tmp, "docker-called")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho called >> "%s"\nexit 0\n' % self.marker)
        os.chmod(stub, 0o755)

    def run_check(self, checkout, extra_env=None):
        os.makedirs(os.path.join(checkout, ".yardr"), exist_ok=True)
        shutil.copy(CHECK, os.path.join(checkout, ".yardr", "check"))
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "HOME": self.home,
        }
        env.update(extra_env or {})
        return subprocess.run(
            ["sh", os.path.join(".yardr", "check")],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_checkout_outside_home_stops_before_docker(self):
        checkout = os.path.join(self.tmp, "elsewhere", "work")
        result = self.run_check(checkout)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("is not under $HOME", result.stderr)
        self.assertIn(checkout, result.stderr)
        self.assertIn(self.home, result.stderr)
        self.assertIn("Colima mounts only the home folder", result.stderr)
        self.assertFalse(os.path.exists(self.marker), "docker was called")

    def test_any_path_escape_hatch_skips_the_guard(self):
        checkout = os.path.join(self.tmp, "elsewhere", "work")
        result = self.run_check(checkout, {"GATE_ANY_PATH": "1"})
        self.assertNotIn("is not under $HOME", result.stderr)

    def test_checkout_under_home_passes_the_guard(self):
        checkout = os.path.join(self.home, "work")
        result = self.run_check(checkout)
        self.assertNotIn("is not under $HOME", result.stderr)


class GateSlotHeader(GateCheckGuard):
    """The gate-slot header (yard-s2mw): a bare run re-runs the file through the shared gate-slot.sh
    once, a run under a held slot goes straight on, and the gate's exit code comes through either way.
    A stub gate-slot.sh under the invented $HOME records its calls; the checkout outside $HOME makes the
    gate itself fail fast with exit 2."""

    def setUp(self):
        super().setUp()
        self.slot_calls = os.path.join(self.tmp, "slot-calls")
        integrations = os.path.join(self.home, "ws_yardr", "integrations")
        os.makedirs(integrations)
        # A header that ignores YARD_GATE_SLOT_HELD would re-enter the stub from inside its own slot and
        # loop forever; the stub exits 97 there instead, so that mistake fails the test fast.
        with open(os.path.join(integrations, "gate-slot.sh"), "w") as f:
            f.write(
                '#!/bin/sh\necho "$*" >> "%s"\n[ -z "${YARD_GATE_SLOT_HELD:-}" ] || exit 97\n'
                'YARD_GATE_SLOT_HELD=1\nexport YARD_GATE_SLOT_HELD\nshift\nexec "$@"\n'
                % self.slot_calls
            )

    def slot_call_lines(self):
        if not os.path.exists(self.slot_calls):
            return []
        with open(self.slot_calls) as f:
            return f.read().splitlines()

    def test_bare_run_takes_one_slot_and_keeps_the_exit_code(self):
        result = self.run_check(os.path.join(self.tmp, "elsewhere", "work"))
        self.assertEqual(self.slot_call_lines(), ["-- sh .yardr/check"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("is not under $HOME", result.stderr)

    def test_run_under_a_held_slot_goes_straight_on(self):
        result = self.run_check(
            os.path.join(self.tmp, "elsewhere", "work"), {"YARD_GATE_SLOT_HELD": "1"}
        )
        self.assertEqual(self.slot_call_lines(), [])
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
