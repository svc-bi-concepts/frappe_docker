"""Offline tests for inventory.py: the report goes to a file under private/, since av inject drops stdout. No network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import os
import stat
import unittest
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(os.path.abspath(__file__))  # run this checkout's files, never the main checkout's
inventory = launcher.load("inventory.py")  # the launcher is a bash file; the tests run its python body
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


class ReadTokenRunTest(unittest.TestCase):
    # inventory.py runs with only BEXIO_READ_TOKEN in the environment: entities
    # the read token cannot reach (403) are reported as "no access (token
    # scope)", the rest of the report is written, nothing crashes. HTTP mocked.

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.requests = 1
            self.token_kind = "pat"

        def paginate(self, path, params=None):
            if path == "/2.0/contact":
                return iter([{"id": 1, "name": "invented contact"}])
            if path == "/2.0/accounts":
                return iter([{"account_no": "1000", "name": "invented account",
                              "account_type": "invented", "tax_id": None, "is_active": True}])
            if path == "/3.0/taxes":
                return iter([{"id": 1, "code": "UT", "name": "invented tax", "value": 8.1,
                              "net_tax_value": None, "type": "sales_tax", "start_year": 2024,
                              "start_month": 1, "end_year": None, "end_month": None,
                              "is_active": True, "account_id": 7}])
            raise inventory.BexioError(403, path)

        def paginate_pages(self, path, size_param, rows_key=None, params=None, page_size=None):
            raise inventory.BexioError(403, path)

    def test_runs_with_only_the_read_token_and_reports_scoped_out_entities(self):
        with mock.patch.dict(os.environ, {"BEXIO_READ_TOKEN": "read-token-not-real"}, clear=True), \
                mock.patch.object(inventory, "Client", self.FakeClient), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            inventory.print_report()
        report = out.getvalue()
        self.assertIn("no access (token scope)", report)
        self.assertIn("| Contacts | contacts | ok |", report)
        self.assertIn("### contacts", report)  # the one reachable entity still has its masked sample


if __name__ == "__main__":
    unittest.main()
