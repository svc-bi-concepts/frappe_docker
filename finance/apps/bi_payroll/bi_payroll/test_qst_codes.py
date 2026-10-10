"""Offline tests of the Quellensteuer tariff code as a link (erp-wvbn), with a stand-in for frappe and invented rows:

    python3 -m unittest bi_payroll.test_qst_codes

from this app's directory. They cover the QST Tariff Code records the loader and after_migrate make (once each, a rerun
makes none), the codes the Employee form offers by canton, and the save check: a code needs its canton, and the pair
must be one that a QST Tariff row carries. A canton alone passes.
"""

import importlib
import json
import sys
import types
import unittest
from datetime import date
from unittest import mock


class Thrown(Exception):
    pass


class FakeDb:
    # rows: the QST Tariff rows that exist, as (canton, tariff_code); codes: the QST Tariff Code records that exist;
    # inserted: the QST Tariff Code records made, in order (the rows the loader inserts are not kept)
    def __init__(self, rows=(), codes=()):
        self.rows = set(rows)
        self.codes = set(codes)
        self.inserted = []

    def exists(self, doctype, filters=None):
        if doctype == "QST Tariff Code":
            return filters in self.codes
        if doctype == "QST Tariff":
            return (filters["canton"], filters["tariff_code"]) in self.rows
        return False

    def delete(self, doctype, filters=None):
        # the loader replaces a canton's rows of the file's valid-from dates; nothing is read back here
        return None


class FakeDoc:
    def __init__(self, db, values):
        self.db, self.values = db, values

    def insert(self, ignore_permissions=False):
        if self.values["doctype"] == "QST Tariff Code":
            self.db.inserted.append(self.values)
            self.db.codes.add(self.values["code"])
        return self


def fake_frappe(db, tariff_rows=()):
    frappe = types.ModuleType("frappe")
    frappe.db = db
    frappe.whitelist = lambda *args, **kwargs: (lambda function: function)
    frappe.commit = mock.Mock()

    def throw(message):
        raise Thrown(message)

    frappe.throw = throw
    frappe.get_doc = lambda values: FakeDoc(db, values)
    frappe.get_all = mock.Mock(side_effect=lambda doctype, **kwargs: list(tariff_rows))
    frappe.db.commit = frappe.commit
    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda value, precision=None: float(value or 0)
    utils.getdate = lambda value: date.fromisoformat(str(value))
    frappe.utils = utils
    return frappe, utils


# the QST Tariff rows of two invented cantons: ZG carries A0N and B2N, ZH carries B2N only
TARIFF = [("ZG", "A0N"), ("ZG", "B2N"), ("ZG", "A0N"), ("ZH", "B2N")]


class Load:
    # imports bi_payroll.swiss_payroll with frappe stood in for, as test_structure_gap does
    def load(self, db, tariff_rows=()):
        frappe, utils = fake_frappe(db, tariff_rows)
        patcher = mock.patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bi_payroll.swiss_payroll", None)
        self.addCleanup(sys.modules.pop, "bi_payroll.swiss_payroll", None)
        return importlib.import_module("bi_payroll.swiss_payroll"), frappe


class Codes(Load, unittest.TestCase):
    def test_each_distinct_code_gets_one_record(self):
        db = FakeDb()
        module, _ = self.load(db)
        self.assertEqual(module.create_tariff_codes(["B2N", "A0N", "B2N"]), 2)
        self.assertEqual([values["code"] for values in db.inserted], ["A0N", "B2N"])

    def test_a_rerun_creates_none(self):
        db = FakeDb(codes=["A0N"])
        module, _ = self.load(db)
        self.assertEqual(module.create_tariff_codes(["A0N", "B2N"]), 1)
        self.assertEqual(module.create_tariff_codes(["A0N", "B2N"]), 0)
        self.assertEqual([values["code"] for values in db.inserted], ["B2N"])

    def test_after_migrate_makes_the_codes_of_the_tariff_rows(self):
        db = FakeDb()
        module, frappe = self.load(db, tariff_rows=["A0N", "B2N"])
        self.assertEqual(module.ensure_tariff_codes(), None)
        self.assertEqual(sorted(db.codes), ["A0N", "B2N"])
        _, kwargs = frappe.get_all.call_args
        self.assertEqual((kwargs["pluck"], kwargs["group_by"]), ("tariff_code", "tariff_code"))

    def test_the_loader_makes_the_codes_of_its_file_once(self):
        db = FakeDb(codes=["A0N"])
        module, _ = self.load(db)
        file_rows = [
            {"canton": "ZG", "tariff_code": "A0N", "valid_from": date(2026, 1, 1)},
            {"canton": "ZG", "tariff_code": "A0N", "valid_from": date(2026, 1, 1)},
            {"canton": "ZG", "tariff_code": "B2N", "valid_from": date(2026, 1, 1)},
        ]
        with mock.patch.object(module, "parse_tariff_file", return_value=file_rows), \
                mock.patch("builtins.open", mock.mock_open(read_data="")):
            self.assertEqual(module.load_tariff("/invented/ZG.txt"), 3)
        self.assertEqual([values["code"] for values in db.inserted], ["B2N"])
        self.assertEqual(db.inserted[0]["doctype"], "QST Tariff Code")


class FormOffers(Load, unittest.TestCase):
    def test_a_canton_offers_its_own_codes(self):
        module, frappe = self.load(FakeDb(), tariff_rows=["A0N", "B2N"])
        self.assertEqual(module.qst_codes_for_canton("ZG"), ["A0N", "B2N"])
        _, kwargs = frappe.get_all.call_args
        self.assertEqual(kwargs["filters"], {"canton": "ZG"})

    def test_no_canton_offers_every_code(self):
        module, frappe = self.load(FakeDb(), tariff_rows=["A0N", "B2N"])
        module.qst_codes_for_canton(None)
        _, kwargs = frappe.get_all.call_args
        self.assertEqual(kwargs["filters"], {})

    def test_the_link_search_takes_the_canton_from_the_filters_and_the_text(self):
        module, _ = self.load(FakeDb(), tariff_rows=["A0N", "B2N", "B2Y"])
        filters = json.dumps({"swiss_qst_canton": "ZG"})  # the form sends the filters as JSON text
        self.assertEqual(module.search_qst_codes("QST Tariff Code", "b2", "code", 0, 10, filters), [("B2N",), ("B2Y",)])
        self.assertEqual(module.search_qst_codes("QST Tariff Code", "", "code", 1, 1, {"swiss_qst_canton": "ZG"}), [("B2N",)])


class SaveCheck(Load, unittest.TestCase):
    def check(self, canton, code, rows=TARIFF):
        module, frappe = self.load(FakeDb(rows=rows))
        module.validate_qst_fields({"swiss_qst_canton": canton, "swiss_qst_code": code})

    def test_a_code_with_its_canton_passes(self):
        self.check("ZG", "B2N")

    def test_a_canton_alone_passes(self):
        self.check("ZG", None)

    def test_nothing_passes(self):
        self.check(None, None)

    def test_a_code_without_a_canton_stops(self):
        with self.assertRaises(Thrown) as caught:
            self.check(None, "B2N")
        self.assertIn("needs its canton", str(caught.exception))

    def test_a_pair_no_tariff_row_carries_stops_and_names_the_canton_and_the_loader(self):
        with self.assertRaises(Thrown) as caught:
            self.check("ZH", "A0N")  # A0N is a code of ZG only
        message = str(caught.exception)
        self.assertIn("A0N", message)
        self.assertIn("ZH", message)
        self.assertIn("bi_payroll.swiss_payroll.load_tariff", message)


if __name__ == "__main__":
    unittest.main()
