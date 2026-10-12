"""Offline tests for gate-image.sh: which image tag the in-image step of .yardr/check runs.
Invented tags, stub git (origin/main's finance-local.yml) and stub docker (the images at rest),
no network. Run with: cd finance/scripts && python3 -m unittest test_gate_image
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "gate-image.sh")
PY = os.path.join(ROOT, "finance", "scripts", "switch_image.py")

COMPOSE = """\
services:
  frontend:
    image: frappe-finance-custom:%s
  backend:
    image: frappe-finance-custom:%s
  scheduler:
    image: frappe-finance-custom:%s
"""


def compose(tag):
    return COMPOSE % (tag, tag, tag)


class GateImage(unittest.TestCase):
    """gate-image.sh in a scratch layout, with stub git and stub docker."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "finance", "scripts"))
        os.makedirs(os.path.join(self.tmp, "bin"))
        shutil.copy(SCRIPT, os.path.join(self.tmp, "finance", "scripts"))
        shutil.copy(PY, os.path.join(self.tmp, "finance", "scripts"))
        self.script = os.path.join(self.tmp, "finance", "scripts", "gate-image.sh")
        self.main_file = os.path.join(self.tmp, "origin-main-finance-local.yml")

    def stub(self, name, body):
        path = os.path.join(self.tmp, "bin", name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_gate(self, branch_tag, main_tag, at_rest):
        """main_tag None: origin/main has no readable finance-local.yml. at_rest: the tags of the
        images docker holds, all in the frappe-finance-custom repository."""
        with open(os.path.join(self.tmp, "finance-local.yml"), "w") as f:
            f.write(compose(branch_tag))
        if main_tag is not None:
            with open(self.main_file, "w") as f:
                f.write(compose(main_tag))
        elif os.path.exists(self.main_file):
            os.remove(self.main_file)
        self.stub(
            "git",
            '[ "$*" = "show origin/main:finance-local.yml" ] && [ -f "%s" ] && { cat "%s"; exit 0; }\n'
            "exit 128\n" % (self.main_file, self.main_file),
        )
        held = " ".join(at_rest)
        self.stub(
            "docker",
            'if [ "$1 $2" = "image inspect" ]; then\n'
            '  t=${3#frappe-finance-custom:}\n'
            '  case " %s " in *" $t "*) exit 0 ;; esac\n'
            "  exit 1\nfi\n"
            'if [ "$1" = "images" ]; then\n'
            "  for t in %s; do echo \"$t\"; done\n  exit 0\nfi\n"
            "exit 0\n" % (held, held),
        )
        env = {"PATH": os.path.join(self.tmp, "bin") + os.pathsep + os.environ["PATH"]}
        return subprocess.run(
            ["sh", self.script], cwd=self.tmp, capture_output=True, text=True, env=env,
        )

    def test_main_pin_present_wins_over_the_branch_pin(self):
        run = self.run_gate("t-branch", "t-main", ["t-main", "t-branch"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "frappe-finance-custom:t-main")
        self.assertIn("rebase onto main", run.stderr)

    def test_branch_pin_when_main_pin_is_gone(self):
        run = self.run_gate("t-branch", "t-main", ["t-branch", "t-other"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "frappe-finance-custom:t-branch")
        self.assertEqual(run.stderr.count("\n"), 1, run.stderr)
        self.assertIn("origin/main pins frappe-finance-custom:t-main", run.stderr)
        self.assertIn("rebase onto main", run.stderr)

    def test_branch_pin_when_origin_main_is_unreadable(self):
        run = self.run_gate("t-branch", None, ["t-branch"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "frappe-finance-custom:t-branch")
        self.assertIn("origin/main pins frappe-finance-custom:(unreadable)", run.stderr)

    def test_one_image_at_rest_when_neither_pin_is_present(self):
        run = self.run_gate("t-branch", "t-main", ["t-new"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "frappe-finance-custom:t-new")
        self.assertIn("rebase onto main", run.stderr)

    def test_refuses_when_no_image_fits(self):
        for at_rest in ([], ["t-a", "t-b"]):
            run = self.run_gate("t-branch", "t-main", at_rest)
            self.assertEqual(run.returncode, 1, at_rest)
            self.assertEqual(run.stdout, "", at_rest)
            self.assertIn("t-main", run.stderr)
            self.assertIn("t-branch", run.stderr)
            self.assertEqual(run.stderr.count("\n"), 1, run.stderr)

    def test_same_pin_on_main_and_branch_is_silent(self):
        run = self.run_gate("t-main", "t-main", ["t-main"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "frappe-finance-custom:t-main")
        self.assertEqual(run.stderr, "")


if __name__ == "__main__":
    unittest.main()
