"""Offline tests of the bexio MWST loader (load_mwst_bexio.py), with a stub Erp and invented figures.

Counts only, no amounts and no names: the repo is public. Standard library only, run by the depot's
light gate (unittest discover in finance/bexio).
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import import_master as im
import load_mwst_bexio as loader
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)

CSV = """quarter,ziffer,label,entgelt_chf,steuer_chf,journal_entgelt_chf,journal_steuer_chf,entgelt_tol_chf,steuer_tol_chf
2026Q1,302,Normalsatz 7.7 %,1000.00,70.00,0.00,0.00,0.10,0.05
2026Q1,400,Vorsteuer,0.00,70.00,0.00,35.00,0.00,0.05
2026Q2,302,Normalsatz 7.7 %,2000.00,140.00,0.00,0.00,0.10,0.05
"""

ROWS = [
    ("2026Q1", "302", Decimal("1000.00"), Decimal("70.00")),
    ("2026Q1", "400", Decimal("0.00"), Decimal("70.00")),
    ("2026Q2", "302", Decimal("2000.00"), Decimal("140.00")),
]


class StubErp:
    """The few Erp calls the loader makes, over an in-memory store: {doctype: {name: doc}}."""

    def __init__(self):
        self.store = {}

    def list(self, doctype, filters=None, fields=("name",)):
        return [{"name": name} for name in self.store.get(doctype, {})]

    def get(self, doctype, name):
        return self.store[doctype][name]

    def insert(self, doctype, doc):
        doc = dict(doc)
        name = loader.name_of(doc["quarter"], doc["ziffer"])
        doc["name"] = name
        self.store.setdefault(doctype, {})[name] = doc
        return {"name": name}

    def update(self, doctype, name, values):
        self.store[doctype][name].update(values)
        return self.store[doctype][name]


def write_csv(text):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class ReadTest(unittest.TestCase):
    def test_the_csv_gives_the_totals_per_quarter_and_ziffer(self):
        path = write_csv(CSV)
        try:
            self.assertEqual(loader.read_rows(path), ROWS)
        finally:
            os.unlink(path)

    def test_a_row_outside_the_form_stops_the_load(self):
        path = write_csv(
            "quarter,ziffer,entgelt_chf,steuer_chf\n2026Q5,302,1.00,0.00\n"
        )
        try:
            with self.assertRaises(ValueError):
                loader.read_rows(path)
        finally:
            os.unlink(path)

    def test_the_name_is_the_deterministic_autoname(self):
        self.assertEqual(loader.name_of("2026Q3", "302"), "MWST-BEXIO-2026Q3-302")


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.erp = StubErp()

    def test_a_first_run_creates_each_row_once(self):
        counts = loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        self.assertEqual(counts["created"], 3)
        stored = self.erp.store["MWST bexio Ziffer"]["MWST-BEXIO-2026Q1-302"]
        self.assertEqual(stored["base"], 1000.00)
        self.assertEqual(stored["source"], "bexio export 2026-10-10")

    def test_a_rerun_changes_nothing(self):
        loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        counts = loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        self.assertEqual(
            (counts["created"], counts["updated"], counts["unchanged"]), (0, 0, 3)
        )

    def test_a_changed_figure_updates_its_row(self):
        loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        changed = [("2026Q1", "302", Decimal("1000.50"), Decimal("70.00"))]
        counts = loader.load(
            self.erp, changed, "bexio export 2026-10-10", dry_run=False
        )
        self.assertEqual((counts["updated"], counts["unchanged"]), (1, 0))
        self.assertEqual(
            self.erp.store["MWST bexio Ziffer"]["MWST-BEXIO-2026Q1-302"]["base"],
            1000.50,
        )

    def test_the_site_is_listed_once_not_once_per_row(self):
        with mock.patch.object(self.erp, "list", wraps=self.erp.list) as listed:
            loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        self.assertEqual(listed.call_count, 1)

    def test_a_dry_run_counts_but_writes_nothing(self):
        counts = loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=True)
        self.assertEqual(counts["created"], 3)
        self.assertEqual(self.erp.store.get("MWST bexio Ziffer", {}), {})

    def test_a_failed_row_is_counted_not_raised(self):
        loader.load(self.erp, ROWS, "bexio export 2026-10-10", dry_run=False)
        with mock.patch.object(im, "differs", side_effect=im.ErpError(500, "update")):
            counts = loader.load(
                self.erp, ROWS, "bexio export 2026-10-10", dry_run=False
            )
        self.assertEqual(counts["failed"], 3)
        self.assertEqual(counts["created"], 0)


if __name__ == "__main__":
    unittest.main()
