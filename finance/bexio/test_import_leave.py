"""Offline leave-import tests using invented Employee records only."""

import copy
import json
import os
import unittest

import import_leave as il


class FakeErp:
    def __init__(self):
        self.docs = {doctype: {} for doctype in ("Holiday List", "Leave Type", "Leave Period", "Employee", "Leave Allocation")}
        self.writes = 0
        self.seq = 0

    def list(self, doctype, filters=None, fields=("name",)):
        rows = []
        for doc in self.docs[doctype].values():
            if all(doc.get(key) == value for key, op, value in filters or []):
                rows.append({key: doc.get(key) for key in fields})
        return rows

    def get(self, doctype, name):
        return copy.deepcopy(self.docs[doctype][name])

    def insert(self, doctype, doc):
        self.writes += 1
        self.seq += 1
        name = doc.get("holiday_list_name") or doc.get("leave_type_name") or "test-{}".format(self.seq)
        self.docs[doctype][name] = dict(copy.deepcopy(doc), doctype=doctype, name=name, docstatus=0)
        return {"name": name}

    def update(self, doctype, name, values):
        self.writes += 1
        self.docs[doctype][name].update(copy.deepcopy(values))

    def call(self, method, **args):
        self.writes += 1
        doc = args.get("doc") or {}
        if doc.get("doctype") == "Leave Allocation":
            self.docs["Leave Allocation"][doc["name"]]["docstatus"] = 1
        return args


class AllocationRuleTest(unittest.TestCase):
    def test_export_days_take_precedence(self):
        days, default = il.allocation_days({"swiss_employment_level": 50}, {"annual_vacation_days_total": 23})
        self.assertEqual((days, default), (23.0, False))

    def test_default_minimum_is_prorated_by_employment_level(self):
        employee = {"swiss_employment_level": 60}
        self.assertEqual(il.allocation_days(employee, {"date_of_birth": "1990-06-01"}), (12.0, True))
        self.assertEqual(il.allocation_days(employee, {"date_of_birth": "2007-06-01"}), (15.0, True))

    def test_missing_level_is_reportable_not_assumed_full_time(self):
        with self.assertRaises(ValueError):
            il.allocation_days({}, {"date_of_birth": "1990-06-01"})

    def test_holidays_include_public_dates_and_all_weekends(self):
        rows = il.holiday_rows()
        by_date = {row["holiday_date"]: row for row in rows}
        self.assertEqual(by_date["2026-01-01"]["description"], "Neujahr")
        self.assertEqual(by_date["2026-12-26"]["description"], "Stephanstag")
        self.assertEqual(by_date["2026-01-03"]["weekly_off"], 1)
        self.assertEqual(len(il.PUBLIC_HOLIDAYS), 13)


class RepeatabilityTest(unittest.TestCase):
    def test_setup_upserts_create_then_second_run_is_unchanged(self):
        erp = FakeErp()
        first = il.LeaveImporter(erp, dry_run=False)
        first.run_setup()
        writes = erp.writes
        second = il.LeaveImporter(erp, dry_run=False)
        second.run_setup()
        self.assertEqual(erp.writes, writes)
        self.assertEqual(second.counts["created"], 0)
        self.assertEqual(second.counts["unchanged"], 3)

    def test_dry_run_writes_nothing_and_plans_setup(self):
        erp = FakeErp()
        importer = il.LeaveImporter(erp, dry_run=True)
        importer.run_setup()
        self.assertEqual(erp.writes, 0)
        self.assertEqual(importer.counts["created"], 3)

    def test_active_employee_gets_one_allocation_and_rerun_creates_none(self):
        erp = FakeErp()
        employee = {"name": "HR-EMP-8001", "employee_name": "A. Example", "status": "Active",
                    "swiss_employment_level": 60}
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        export = {"id": "8001", "employment_level": 60, "date_of_birth": "1990-01-01",
                  "annual_vacation_days_total": 22}
        plan, defaults, problems = il.build_plan([export], {"8001": employee})
        self.assertEqual((len(plan), len(defaults), problems), (1, 0, []))
        first = il.LeaveImporter(erp, dry_run=False)
        first.run_employees(plan)
        self.assertEqual(first.counts["created"], 1)
        self.assertEqual(erp.docs["Leave Allocation"]["test-1"]["new_leaves_allocated"], 22)
        second = il.LeaveImporter(erp, dry_run=False)
        second.run_employees(plan)
        self.assertEqual(second.counts["created"], 0)
        self.assertEqual(second.counts["updated"], 0)
        self.assertEqual(len(erp.docs["Leave Allocation"]), 1)


class FixtureTest(unittest.TestCase):
    def test_custom_fields_exist_in_order_and_hook_filter_covers_them(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "apps", "bi_payroll", "bi_payroll"))
        with open(os.path.join(root, "fixtures", "custom_field.json"), encoding="utf-8") as stream:
            fields = json.load(stream)
        by_name = {row["name"]: row for row in fields}
        names = ["Employee-swiss_employment_level", "Employee-swiss_hours_per_week", "Employee-swiss_stay_permit"]
        self.assertEqual([by_name[name]["insert_after"] for name in names],
                         ["swiss_qst_code", "swiss_employment_level", "swiss_hours_per_week"])
        self.assertEqual([by_name[name]["fieldtype"] for name in names], ["Percent", "Float", "Select"])
        self.assertTrue(all(by_name[name]["read_only"] == 0 for name in names))
        with open(os.path.join(root, "hooks.py"), encoding="utf-8") as stream:
            self.assertIn('"like", "Employee-swiss_%"', stream.read())


if __name__ == "__main__":
    unittest.main()
