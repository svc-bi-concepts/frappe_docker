"""Keep the bi_finance modules split exactly once across the two gate lines."""

import io
import os
import re
import shlex
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULES = os.path.join(ROOT, "finance", "apps", "bi_finance", "bi_finance")
HOST_GATE = os.path.join(ROOT, ".yardr", "check-host.sh")
IMAGE_GATE = os.path.join(ROOT, "finance", "scripts", "gate-modules.sh")
MODULE_PATTERN = re.compile(r"\bbi_finance\.(test_[A-Za-z0-9_]+)\b")


def modules_on_unittest_line(path):
    with open(path) as source:
        for line in source:
            if line.lstrip().startswith("#"):
                continue
            if "-m unittest" in line and "bi_finance.test_" in line:
                line = " ".join(shlex.split(line, comments=True))
                return set(MODULE_PATTERN.findall(line))
    raise AssertionError("no bi_finance unittest command in %s" % path)


class GateLineParsing(unittest.TestCase):
    def test_commented_command_does_not_hide_active_command(self):
        source = (
            "  # python3 -m unittest bi_finance.test_commented\n"
            "(cd finance/apps/bi_finance && python3 -m unittest bi_finance.test_mt940)\n"
        )
        with mock.patch("builtins.open", return_value=io.StringIO(source)):
            self.assertEqual(modules_on_unittest_line("host"), {"test_mt940"})

    def test_inline_comment_does_not_add_a_module(self):
        source = (
            "sh -c 'python -m unittest bi_finance.test_mt940' "
            "# bi_finance.test_commented\n"
        )
        with mock.patch("builtins.open", return_value=io.StringIO(source)):
            self.assertEqual(modules_on_unittest_line("image"), {"test_mt940"})


class GateModules(unittest.TestCase):
    def test_every_module_is_named_in_exactly_one_gate_line(self):
        on_disk = {
            os.path.splitext(name)[0]
            for name in os.listdir(MODULES)
            if name.startswith("test_") and name.endswith(".py")
        }
        host = modules_on_unittest_line(HOST_GATE)
        with open(IMAGE_GATE) as manifest:
            words = shlex.split(manifest.read(), comments=True)
        image = set(MODULE_PATTERN.findall(" ".join(words)))
        self.assertEqual(
            len(image), len([word for word in words if word.startswith("bi_finance.")])
        )

        for module in sorted(host - on_disk):
            self.fail("%s is named in host list but has no module on disk" % module)
        for module in sorted(image - on_disk):
            self.fail("%s is named in image list but has no module on disk" % module)
        for module in sorted(on_disk):
            count = int(module in host) + int(module in image)
            if count != 1:
                lists = []
                if module not in host:
                    lists.append("host list")
                if module not in image:
                    lists.append("image list")
                issue = (
                    "not named in " + " or ".join(lists)
                    if count == 0
                    else "named in both host list and image list"
                )
                self.fail(f"{module} must appear in exactly one gate list; {issue}")


if __name__ == "__main__":
    unittest.main()
