"""Offline leave-import tests using invented Employee records only."""

import copy
import json
import os
import unittest

import import_leave as il


class FakeErp:
    def __init__(self):
        self.docs = {doctype: {} for doctype in ("Holiday List", "Leave Type", "Leave Period", "Employee",
                                                 "Leave Allocation", "Holiday List Assignment")}
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
        if doc.get("doctype") in self.docs and doc.get("name") in self.docs[doc["doctype"]]:
            self.docs[doc["doctype"]][doc["name"]]["docstatus"] = 1
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
        allocation = next(iter(erp.docs["Leave Allocation"].values()))
        self.assertEqual(allocation["new_leaves_allocated"], 22)
        second = il.LeaveImporter(erp, dry_run=False)
        second.run_employees(plan)
        self.assertEqual(second.counts["created"], 0)
        self.assertEqual(second.counts["updated"], 0)
        self.assertEqual(len(erp.docs["Leave Allocation"]), 1)


def active_employee(name="HR-EMP-8001", level=60, status="Active"):
    return {"name": name, "employee_name": "A. Example", "status": status, "swiss_employment_level": level}


class PlanAndAllocationTest(unittest.TestCase):
    def test_plan_skips_left_reports_unknown_and_lists_defaults(self):
        export = [{"id": "8001", "employment_level": 60, "date_of_birth": "1990-01-01"},
                  {"id": "8002", "employment_level": 100, "date_of_birth": "1990-01-01"},
                  {"id": "8003", "employment_level": 100},
                  {"id": "8004", "date_of_birth": "1990-01-01"}]
        erp = {"8001": active_employee("HR-EMP-8001"), "8002": active_employee("HR-EMP-8002", 100, "Left"),
               "8004": {"name": "HR-EMP-8004", "status": "Active"}}
        plan, defaults, problems = il.build_plan(export, erp)
        self.assertEqual([entry["position"] for entry in plan], [1])
        self.assertEqual(len(defaults), 1)
        self.assertEqual(sorted(problems), [(3, "Employee not found"), (4, "default allocation needs employment level")])

    def test_apply_submits_the_allocation_and_sets_the_employee_holiday_list(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        plan, _defaults, _problems = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
        importer = il.LeaveImporter(erp, dry_run=False)
        importer.run_employees(plan)
        allocation = next(iter(erp.docs["Leave Allocation"].values()))
        self.assertEqual(allocation["docstatus"], 1)
        self.assertEqual(erp.docs["Employee"][employee["name"]]["holiday_list"], il.HOLIDAY_LIST)

    def test_assignment_is_created_submitted_and_unchanged_on_rerun(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        plan, _d, _p = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
        first = il.LeaveImporter(erp, dry_run=False)
        first.run_employees(plan)
        assignment = next(iter(erp.docs["Holiday List Assignment"].values()))
        self.assertEqual(assignment["docstatus"], 1)
        self.assertEqual(assignment["applicable_for"], "Employee")
        self.assertEqual(assignment["assigned_to"], employee["name"])
        self.assertEqual((assignment["holiday_list"], assignment["from_date"]), (il.HOLIDAY_LIST, il.FROM_DATE))
        self.assertEqual((first.counts["assignments planned"], first.counts["assignments created"]), (1, 1))
        second = il.LeaveImporter(erp, dry_run=False)
        second.run_employees(plan)
        self.assertEqual((second.counts["assignments created"], second.counts["assignments unchanged"]), (0, 1))
        self.assertEqual(len(erp.docs["Holiday List Assignment"]), 1)

    def test_cancelled_assignment_does_not_block_a_new_one(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        erp.docs["Holiday List Assignment"]["old"] = {
            "name": "old", "doctype": "Holiday List Assignment", "docstatus": 2,
            "naming_series": il.ASSIGNMENT_SERIES, "applicable_for": "Employee",
            "assigned_to": employee["name"], "holiday_list": il.HOLIDAY_LIST, "from_date": il.FROM_DATE}
        plan, _d, _p = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
        importer = il.LeaveImporter(erp, dry_run=False)
        importer.run_employees(plan)
        self.assertEqual(importer.counts["assignments created"], 1)
        self.assertEqual(len(erp.docs["Holiday List Assignment"]), 2)

    def test_zero_days_creates_no_allocation_and_is_counted_not_failed(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        plan, defaults, problems = il.build_plan([{"id": "8001", "annual_vacation_days_total": 0}], {"8001": employee})
        self.assertEqual((plan[0]["days"], defaults, problems), (0.0, [], []))
        importer = il.LeaveImporter(erp, dry_run=False)
        importer.run_employees(plan)
        self.assertEqual(len(erp.docs["Leave Allocation"]), 0)
        self.assertEqual(importer.counts["no entitlement"], 1)
        self.assertEqual(importer.counts["failed"], 0)
        self.assertEqual(importer.problems, [])
        self.assertEqual(len(erp.docs["Holiday List Assignment"]), 1)

    def test_draft_allocation_is_flagged_not_replaced_and_cancelled_one_does_not_block(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        base = {"employee": employee["name"], "leave_type": il.LEAVE_TYPE, "from_date": il.FROM_DATE,
                "to_date": il.TO_DATE, "new_leaves_allocated": 20}
        erp.docs["Leave Allocation"]["draft"] = dict(base, name="draft", docstatus=0)
        plan, _d, _p = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
        importer = il.LeaveImporter(erp, dry_run=False)
        importer.run_employees(plan)
        self.assertEqual(len(erp.docs["Leave Allocation"]), 1)
        self.assertEqual(importer.problems, [(1, "matching Leave Allocation is draft")])
        erp.docs["Leave Allocation"]["draft"]["docstatus"] = 2
        again = il.LeaveImporter(erp, dry_run=False)
        again.run_employees(plan)
        self.assertEqual(again.counts["created"], 1)

    def test_apply_counts_the_employee_holiday_list_update_as_the_dry_run_does(self):
        # the dry run is what the go-live reads before the apply: both count the same update
        counts = []
        for dry_run in (True, False):
            erp = FakeErp()
            employee = active_employee()
            erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
            plan, _d, _p = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
            importer = il.LeaveImporter(erp, dry_run=dry_run)
            importer.run_employees(plan)
            counts.append(importer.counts["updated"])
        self.assertEqual(counts, [1, 1])

    def test_dry_run_employees_writes_nothing(self):
        erp = FakeErp()
        employee = active_employee()
        erp.docs["Employee"][employee["name"]] = copy.deepcopy(employee)
        plan, _d, _p = il.build_plan([{"id": "8001", "annual_vacation_days_total": 25}], {"8001": employee})
        importer = il.LeaveImporter(erp, dry_run=True)
        importer.run_employees(plan)
        self.assertEqual(erp.writes, 0)
        self.assertEqual(importer.counts["created"], 1)
        self.assertEqual((importer.counts["assignments planned"], importer.counts["assignments created"]), (1, 1))
        self.assertEqual(len(erp.docs["Holiday List Assignment"]), 0)

    def test_defaults_file_is_private(self):
        import stat
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "payroll", "leave-defaults.txt")
        il.write_defaults(path, [(active_employee(), 12.0)])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


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
