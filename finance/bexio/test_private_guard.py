"""Tests of the private_guard fixture: a test that writes into the real private folder fails.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import os
import unittest

import private_guard
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)


class GuardTest(unittest.TestCase):
    def test_a_write_into_the_real_private_folder_is_refused(self):
        write = private_guard._guarded(
            lambda path, lines: None, os.path.realpath(private_guard.REAL)
        )
        with self.assertRaises(AssertionError):
            write(os.path.join(private_guard.REAL, "a-test-wrote-here.txt"), ["x"])

    def test_the_fixture_points_the_modules_at_a_temp_folder(self):
        import import_master as im

        self.assertNotEqual(im.PRIVATE, private_guard.REAL)
        self.assertTrue(os.path.isdir(im.PRIVATE))


if __name__ == "__main__":
    unittest.main()
