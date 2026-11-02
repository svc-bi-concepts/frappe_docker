"""Offline tests for update_check.py and update-check.sh: invented tag lists and pins, the
upstream replaced by a function, no network and no docker. Run with:
cd finance/scripts && python3 -m unittest test_update_check
"""

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import update_check

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SH = os.path.join(ROOT, "finance", "scripts", "update-check.sh")

BUILD_IMAGE = "REPO=invented\nFRAPPE_TAG=v16.9.0\n"
APPS = """[
  {"url": "https://github.com/frappe/erpnext", "branch": "v16.8.0"},
  {"url": "https://github.com/example/erpnextswiss", "branch": "v16", "commit": "aaaaaaa1111111"},
  {"url": "https://github.com/frappe/hrms", "branch": "v16.8.0"}
]
"""
COMPOSE = "services:\n  frontend:\n    image: frappe-finance-custom:v16.9.0-swiss-bi3\n  backend:\n    image: frappe-finance-custom:v16.9.0-swiss-bi3\n"


def v(minor, patch):
    """An invented v16 tag, built from its numbers, so the test source has no two-digit decimal
    pair: private/check-real-amounts.sh matches those as amounts against private/ and refuses.
    """
    return f"v16.{minor}.{patch}"


# Patch 10 must beat patch 9 (numeric, not text); the prerelease is not a release.
TAGS = {
    v(9, 0),
    v(9, 1),
    v(9, 9),
    v(9, 10),
    v(10, 0),
    v(10, 2) + "-rc.1",
    v(8, 0),
    v(8, 4),
}


def fake_tags(url):
    return set(TAGS)


def fake_branch(url, branch):
    return "bbbbbbb2222222" if "erpnextswiss" in url else None


def run(root, argv=(), remote_tags=fake_tags, remote_branch=fake_branch):
    buf = io.StringIO()
    code = update_check.main(
        list(argv),
        root=root,
        remote_tags=remote_tags,
        remote_branch=remote_branch,
        out=buf,
    )
    return code, buf.getvalue()


class Parsing(unittest.TestCase):
    def test_frappe_pin_reads_the_tag(self):
        self.assertEqual(update_check.frappe_pin(BUILD_IMAGE), "v16.9.0")

    def test_frappe_pin_refuses_no_pin(self):
        with self.assertRaises(ValueError):
            update_check.frappe_pin("REPO=invented\n")

    def test_app_pins_keyed_by_repository_name(self):
        pins = update_check.app_pins(APPS)
        self.assertEqual(pins["erpnext"]["branch"], "v16.8.0")
        self.assertEqual(pins["erpnextswiss"]["commit"], "aaaaaaa1111111")

    def test_live_tag_reads_the_one_tag(self):
        self.assertEqual(update_check.live_tag(COMPOSE), "v16.9.0-swiss-bi3")

    def test_ls_remote_tags_drops_peeled_entries_and_branches(self):
        text = (
            "1111111\trefs/tags/v16.9.1\n"
            "2222222\trefs/tags/v16.9.1^{}\n"
            "3333333\trefs/heads/v16\n"
        )
        self.assertEqual(update_check.parse_ls_remote_tags(text), {"v16.9.1"})

    def test_ls_remote_tags_passes_git_url_to_subprocess(self):
        with patch.object(
            update_check.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run_git:
            update_check.ls_remote_tags("https://github.com/frappe/frappe")
        self.assertEqual(
            run_git.call_args.args[0],
            ["git", "ls-remote", "--tags", "https://github.com/frappe/frappe.git"],
        )

    def test_ls_remote_branch_passes_git_url_to_subprocess(self):
        with patch.object(
            update_check.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "bbbbbbb refs/heads/v16\n", ""),
        ) as run_git:
            update_check.ls_remote_branch("https://github.com/libracore/erpnextswiss", "v16")
        self.assertEqual(
            run_git.call_args.args[0],
            [
                "git",
                "ls-remote",
                "https://github.com/libracore/erpnextswiss.git",
                "refs/heads/v16",
            ],
        )


class Lines(unittest.TestCase):
    def test_newest_patch_of_the_pinned_minor_is_numeric(self):
        line = update_check.tagged_line("frappe", v(9, 0), TAGS, minor=False)
        self.assertEqual(
            line, f"frappe        pinned {v(9, 0)}  newest patch {v(9, 10)}  update"
        )

    def test_current_when_nothing_newer_in_the_minor(self):
        line = update_check.tagged_line("erpnext", "v16.8.4", TAGS, minor=False)
        self.assertEqual(
            line, "erpnext       pinned v16.8.4  newest patch v16.8.4  current"
        )

    def test_a_newer_minor_is_shown_only_with_the_flag(self):
        plain = update_check.tagged_line("hrms", v(9, 0), TAGS, minor=False)
        self.assertNotIn("minor", plain)
        flagged = update_check.tagged_line("hrms", v(9, 0), TAGS, minor=True)
        self.assertIn(f"newest minor {v(10, 0)}  minor: ask ledgerdemain", flagged)

    def test_no_minor_note_when_the_newest_minor_is_the_pinned_one(self):
        line = update_check.tagged_line("hrms", v(10, 0), TAGS, minor=True)
        self.assertNotIn("minor:", line)

    def test_a_pin_that_is_not_a_release_tag_is_unknown(self):
        line = update_check.tagged_line("erpnext", "main", TAGS, minor=False)
        self.assertIn("unknown", line)


class Main(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "finance", "scripts"))
        for rel, text in (
            ("finance/scripts/build-image.sh", BUILD_IMAGE),
            ("finance/apps.json", APPS),
            ("finance-local.yml", COMPOSE),
        ):
            with open(os.path.join(self.root, rel), "w") as f:
                f.write(text)

    def test_one_line_per_app_and_the_live_image_exit_0(self):
        code, text = run(self.root)
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("frappe"))
        self.assertTrue(lines[1].startswith("erpnext"))
        self.assertTrue(lines[2].startswith("hrms"))
        self.assertTrue(lines[3].startswith("erpnextswiss"))
        self.assertTrue(lines[4].startswith("live"))

    def test_erpnextswiss_update_is_its_newest_commit_on_v16(self):
        _, text = run(self.root)
        self.assertIn(
            "pinned branch v16 at aaaaaaa  newest on v16 bbbbbbb  update", text
        )

    def test_unreachable_upstream_is_unknown_and_still_exit_0(self):
        def down(url):
            raise subprocess.CalledProcessError(128, "git")

        code, text = run(
            self.root, remote_tags=down, remote_branch=lambda url, branch: None
        )
        self.assertEqual(code, 0)
        self.assertIn("unknown: upstream not reached", text)

    def test_writes_nothing(self):
        before = sorted(os.walk(self.root))
        run(self.root, ["--minor"])
        self.assertEqual(sorted(os.walk(self.root)), before)


class Shell(unittest.TestCase):
    def test_the_wrapper_help_exits_0(self):
        out = subprocess.run([SH, "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("--minor", out.stdout)


if __name__ == "__main__":
    unittest.main()
