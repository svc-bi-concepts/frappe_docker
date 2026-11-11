"""Offline tests for the top of .yardr/check. The checkout guard: a checkout outside $HOME stops
the gate before any docker call (Colima mounts only the home folder); only the guard runs in the
'outside' case. The daemon check (erp-xfem): the gate asks docker once, through
finance/scripts/docker-ready.sh, and stops with one line naming the endpoint when no daemon answers,
before any Docker step; it never skips them. Invented folders under a temp dir, stub docker that
records its calls, no network. Run with: cd finance/scripts && python3 -m unittest test_gate_check
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHECK = os.path.join(ROOT, ".yardr", "check")
READY = os.path.join(ROOT, "finance", "scripts", "docker-ready.sh")
MESSAGE = "docker daemon not reachable at %s; on the mini ask IT, don't start or stop runtimes"

# Logs every call with the DOCKER_HOST it saw. info is the daemon question (STUB_INFO_RC=1: no
# daemon answers); context inspect names the current context's endpoint (STUB_CONTEXT_RC=1: it
# cannot); everything else succeeds.
STUB_DOCKER = """#!/bin/sh
echo "$* | DOCKER_HOST=${DOCKER_HOST-unset}" >>"$DOCKER_LOG"
case "$1" in
  info) exit ${STUB_INFO_RC:-0} ;;
  context)
    [ "${STUB_CONTEXT_RC:-0}" = 0 ] || exit 1
    echo "unix:///invented/context.sock" ;;
esac
exit 0
"""


def write_exec(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)


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


class DockerReady(unittest.TestCase):
    """finance/scripts/docker-ready.sh alone: the one question and the one line."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "docker.log")
        write_exec(os.path.join(self.bin, "docker"), STUB_DOCKER)

    def run_ready(self, **extra):
        env = {"PATH": self.bin + os.pathsep + "/usr/bin:/bin", "DOCKER_LOG": self.log}
        env.update(extra)
        return subprocess.run(["sh", READY], env=env, capture_output=True, text=True)

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_answering_daemon_is_silent(self):
        r = self.run_ready()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.calls(), ["info | DOCKER_HOST=unset"])

    def test_silent_daemon_names_the_context_endpoint(self):
        r = self.run_ready(STUB_INFO_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, MESSAGE % "unix:///invented/context.sock" + "\n")

    def test_unreadable_context_falls_back_to_its_name(self):
        r = self.run_ready(STUB_INFO_RC="1", STUB_CONTEXT_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, MESSAGE % "the default context" + "\n")

    def test_a_set_docker_host_is_honoured_and_named(self):
        host = "tcp://127.0.0.1:1"
        r = self.run_ready(STUB_INFO_RC="1", DOCKER_HOST=host)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, MESSAGE % host + "\n")
        # docker saw the caller's DOCKER_HOST unchanged, and no context was looked up
        self.assertEqual(self.calls(), ["info | DOCKER_HOST=" + host])


class GateCheckDocker(unittest.TestCase):
    """The whole of .yardr/check in a checkout under an invented $HOME, with the steps it calls
    stubbed: the host checks, the image pick, its own offline test, pre-commit and docker.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.checkout = os.path.join(self.home, "work")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "docker.log")
        self.steps = os.path.join(self.tmp, "steps.log")
        co = self.checkout
        os.makedirs(os.path.join(co, ".yardr"))
        shutil.copy(CHECK, os.path.join(co, ".yardr", "check"))
        os.makedirs(os.path.join(co, "finance", "scripts"))
        shutil.copy(READY, os.path.join(co, "finance", "scripts", "docker-ready.sh"))
        write_exec(
            os.path.join(co, ".yardr", "check-host.sh"),
            '#!/bin/sh\necho host >>"$STEPS_LOG"\n',
        )
        write_exec(
            os.path.join(co, "finance", "scripts", "gate-image.sh"),
            "#!/bin/sh\necho invented-image:1\n",
        )
        write_exec(
            os.path.join(co, "finance", "scripts", "test_gate_check.py"),
            "import os, unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ran(self):\n"
            '        open(os.environ["STEPS_LOG"], "a").write("offline\\n")\n',
        )
        write_exec(os.path.join(self.bin, "docker"), STUB_DOCKER)
        write_exec(
            os.path.join(self.bin, "pre-commit"),
            '#!/bin/sh\necho pre-commit >>"$STEPS_LOG"\n',
        )

    def run_check(self, **extra):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "HOME": self.home,
            "DOCKER_LOG": self.log,
            "STEPS_LOG": self.steps,
        }
        env.update(extra)
        return subprocess.run(
            ["sh", os.path.join(".yardr", "check")],
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def test_daemon_up_runs_every_docker_step(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.lines(self.log)
        self.assertEqual(calls[0], "info | DOCKER_HOST=unset")
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[1].startswith("run --rm "), calls)
        self.assertIn(" invented-image:1 sh ", calls[1])
        self.assertEqual(self.lines(self.steps), ["host", "offline", "pre-commit"])

    def test_daemon_down_stops_the_gate_before_any_docker_step(self):
        r = self.run_check(STUB_INFO_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, MESSAGE % "unix:///invented/context.sock" + "\n")
        self.assertEqual(
            [c.split(" ")[0] for c in self.lines(self.log)], ["info", "context"]
        )
        self.assertEqual(self.lines(self.steps), [], "a step ran without a daemon")

    def test_a_set_docker_host_reaches_every_docker_step(self):
        host = "tcp://127.0.0.1:1"
        r = self.run_check(DOCKER_HOST=host)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.lines(self.log)
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertTrue(call.endswith("| DOCKER_HOST=" + host), call)

    def test_a_set_docker_host_is_named_when_it_does_not_answer(self):
        host = "tcp://127.0.0.1:1"
        r = self.run_check(DOCKER_HOST=host, STUB_INFO_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, MESSAGE % host + "\n")


if __name__ == "__main__":
    unittest.main()
