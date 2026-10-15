"""Offline tests for load_swiss_rates.py: the CSV parsing and the container-side checks, no frappe.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
Every rate here is invented; the real ones never leave private/.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.modules.setdefault("frappe", mock.MagicMock())

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load_swiss_rates.py")
_spec = importlib.util.spec_from_file_location("load_swiss_rates", _path)
load_swiss_rates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_swiss_rates)

# bi_payroll's swiss_rates.py is pure Python; the loader's field list must match it.
_bp_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "apps",
    "bi_payroll",
    "bi_payroll",
    "swiss_rates.py",
)
_bp_spec = importlib.util.spec_from_file_location("bi_swiss_rates", _bp_path)
bi_swiss_rates = importlib.util.module_from_spec(_bp_spec)
_bp_spec.loader.exec_module(bi_swiss_rates)


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rates-test-")

    def csv(self, rows):
        path = os.path.join(self.dir, "rates.csv")
        with open(path, "w") as f:
            f.write("field,value,unit,set,sheet,page,note\n")
            f.write("\n".join(rows) + "\n")
        return path

    def test_set_yes_rows_become_floats_and_set_no_rows_are_skipped(self):
        path = self.csv(
            [
                "swiss_ktg_ee,0,percent,yes,ktg sheet,3,employer pays",
                "swiss_ktg_ag,2.5,percent,yes,ktg sheet,3,invented rate",
                "ktg_waiting_days,33,days,no,ktg sheet,3,info only",
            ]
        )
        self.assertEqual(
            load_swiss_rates.prepare(path),
            [
                {"field": "swiss_ktg_ee", "value": 0.0},
                {"field": "swiss_ktg_ag", "value": 2.5},
            ],
        )

    def test_a_field_outside_the_rate_list_is_refused(self):
        path = self.csv(["swiss_some_made_up,1.0,percent,yes,ktg sheet,3,oops"])
        with self.assertRaises(ValueError):
            load_swiss_rates.prepare(path)

    def test_a_value_that_is_not_a_number_is_refused(self):
        path = self.csv(["swiss_ktg_ag,2,5,percent,yes,ktg sheet,3,broken"])
        with self.assertRaises(ValueError):
            load_swiss_rates.prepare(path)

    def test_no_set_yes_row_is_an_error(self):
        path = self.csv(["ktg_waiting_days,33,days,no,ktg sheet,3,info only"])
        with self.assertRaises(ValueError):
            load_swiss_rates.prepare(path)

    def test_the_field_lists_agree_with_bi_payroll(self):
        self.assertEqual(load_swiss_rates.RATE_FIELDS, list(bi_swiss_rates.RATE_FIELDS))


class FakeSettingsDoc:
    """Stand-in for the Payroll Swiss Settings single: records what would be saved."""

    def __init__(self, values=None):
        self.values = values or {}
        self.saved = []

    def get(self, field):
        return self.values.get(field)

    def set(self, field, value):
        self.values[field] = value

    def save(self):
        self.saved.append(dict(self.values))


class ContainerTest(unittest.TestCase):
    def run_container(self, rows, values=None):
        """Container mode with frappe stood in; returns (doc, stdout)."""
        doc = FakeSettingsDoc(values)
        fake_frappe = mock.MagicMock()
        fake_frappe.get_doc.return_value = doc
        out = io.StringIO()
        with mock.patch.dict(
            os.environ, {"RATES_JSON": json.dumps(rows)}
        ), mock.patch.dict(sys.modules, {"frappe": fake_frappe}), mock.patch(
            "sys.stdout", out
        ):
            load_swiss_rates.container_apply(dry=False)
        return doc, out.getvalue()

    def test_apply_sets_the_rows_and_saves_once(self):
        doc, out = self.run_container(
            [{"field": "swiss_ktg_ag", "value": 2.5}], values={"swiss_ktg_ee": 0.0}
        )
        self.assertEqual(doc.saved, [{"swiss_ktg_ee": 0.0, "swiss_ktg_ag": 2.5}])
        self.assertIn("swiss_ktg_ag", out)
        # No rate on stdout: the values are read back on the site, not printed.
        self.assertNotIn("2.5", out)

    def test_a_value_already_at_its_target_is_not_touched_again(self):
        doc, out = self.run_container(
            [{"field": "swiss_ktg_ee", "value": 0.0}], values={"swiss_ktg_ee": 0.0}
        )
        self.assertEqual(doc.saved, [{"swiss_ktg_ee": 0.0}])
        self.assertIn("none", out)

    def test_dry_mode_saves_nothing(self):
        doc = FakeSettingsDoc()
        fake_frappe = mock.MagicMock()
        fake_frappe.get_doc.return_value = doc
        with mock.patch.dict(
            os.environ,
            {"RATES_JSON": json.dumps([{"field": "swiss_ktg_ag", "value": 2.5}])},
        ), mock.patch.dict(sys.modules, {"frappe": fake_frappe}), mock.patch(
            "sys.stdout", io.StringIO()
        ):
            load_swiss_rates.container_apply(dry=True)
        self.assertEqual(doc.saved, [])

    def test_an_unknown_field_in_the_json_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_container([{"field": "swiss_not_a_field", "value": 1.0}])


if __name__ == "__main__":
    unittest.main()
