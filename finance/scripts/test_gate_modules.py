"""Keep the bi_finance modules split exactly once across the two gate lines."""

import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULES = os.path.join(ROOT, "finance", "apps", "bi_finance", "bi_finance")
HOST_GATE = os.path.join(ROOT, ".yardr", "check-host.sh")
IMAGE_GATE = os.path.join(ROOT, ".yardr", "check")
MODULE_PATTERN = re.compile(r"\bbi_finance\.(test_[A-Za-z0-9_]+)\b")


def modules_on_unittest_line(path):
    with open(path) as source:
        for line in source:
            if "-m unittest" in line and "bi_finance.test_" in line:
                return set(MODULE_PATTERN.findall(line))
    raise AssertionError("no bi_finance unittest command in %s" % path)


class GateModules(unittest.TestCase):
    def test_every_module_is_named_in_exactly_one_gate_line(self):
        on_disk = {
            os.path.splitext(name)[0]
            for name in os.listdir(MODULES)
            if name.startswith("test_") and name.endswith(".py")
        }
        host = modules_on_unittest_line(HOST_GATE)
        image = modules_on_unittest_line(IMAGE_GATE)

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
                issue = "not named in " + " or ".join(lists) if count == 0 else "named in both host list and image list"
                self.fail("%s must appear in exactly one gate list; %s" % (module, issue))


if __name__ == "__main__":
    unittest.main()
