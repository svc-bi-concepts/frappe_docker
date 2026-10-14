"""Offline tests for inventory.py: the report goes to a file under private/, since av inject drops stdout. No network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import os
import stat
import unittest
from unittest import mock

import inventory
from private_guard import setUpModule  # every test below runs against a temp private folder, never the real one


class ReportFileTest(unittest.TestCase):
    def test_the_report_lands_in_private_with_owner_only_mode(self):
        with mock.patch.object(inventory, "print_report", side_effect=lambda: print("# report invented")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            inventory.main()
        path = os.path.join(inventory.PRIVATE, inventory.REPORT_FOLDER, inventory.REPORT_FILE)
        with open(path) as f:
            self.assertEqual(f.read(), "# report invented\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn(path, err.getvalue())

    def test_a_second_run_replaces_the_report(self):
        for text in ("# first invented\n", "# second invented\n"):
            with mock.patch.object(inventory, "print_report", side_effect=lambda text=text: print(text, end="")), \
                    contextlib.redirect_stderr(io.StringIO()):
                inventory.main()
        path = os.path.join(inventory.PRIVATE, inventory.REPORT_FOLDER, inventory.REPORT_FILE)
        with open(path) as f:
            self.assertEqual(f.read(), "# second invented\n")


if __name__ == "__main__":
    unittest.main()
