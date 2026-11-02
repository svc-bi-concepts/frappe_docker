"""Offline salary-history importer tests with invented paystub figures only."""

import csv
import os
import tempfile
import unittest
from decimal import Decimal

import import_salary_history as ish


def records():
    rows = []
    # Invented totals: earnings 1000.00, deductions 100.00, net 900.00,
    # with expenses and a rent deduction paid after net.
    values = [
        ("1000", "900.00"),
        ("1001", "100.00"),
        ("1005", "-10.00"),
        ("1160", "5.00"),
        ("1218", "5.00"),
        ("5010", "-40.00"),
        ("5020", "-10.00"),
        ("5040", "-10.00"),
        ("5050", "-40.00"),
        ("5060", "0.00"),
        ("6030", "7.00"),
        ("6070", "3.00"),
        ("6300.1", "-6.00"),
        ("6510", "-1.00"),
        ("5000", "1000.00"),
        ("5900", "-100.00"),
        ("6500", "900.00"),
        ("6600", "903.00"),
    ]
    for wage_type, amount in values:
        rows.append(
            {
                "employee_id": "invented-1",
                "year": "2026",
                "month": "10",
                "wage_type": wage_type,
                "amount": amount,
            }
        )
    return rows


class FakeErp:
    def __init__(self):
        self.calls = []
        self.employees = [{"name": "EMP-TEST", "bexio_id": "invented-1"}]
        self.slips = []

    def list(self, doctype, filters=None, fields=("name",)):
        if doctype == "Employee":
            return self.employees
        if doctype == "Salary Slip":
            if filters and filters[0][0] == "swiss_bexio_id" and filters[0][1] != "is":
                return [
                    s for s in self.slips if s.get("swiss_bexio_id") == filters[0][2]
                ]
            return self.slips
        if doctype == "Salary Detail":
            return [{"name": str(i)} for i in range(self.row_count)]
        return []

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        p = kwargs["slip"]
        self.row_count = sum(len(p[k]) for k in ("earnings", "deductions"))
        self.slips.append(
            {
                "name": "SAL-TEST",
                "swiss_bexio_id": p["bexio_id"],
                "gross_pay": p["gross_pay"],
                "total_deduction": p["total_deduction"],
                "net_pay": p["net_pay"],
                "rounded_total": p["paid_out"],
                "is_proposed": 1,
                "row_count": self.row_count,
            }
        )


class SalaryHistoryTests(unittest.TestCase):
    def test_wage_map_signs_totals_and_after_net_rows(self):
        grouped = ish.group_figures(records())
        stub = next(iter(grouped.values()))
        self.assertTrue(ish.sums_hold(stub))
        row = next(r for r in stub["rows"] if r["salary_component"] == "Hourly Wage")
        self.assertEqual(Decimal("-10.00"), Decimal(row["amount"]))
        self.assertFalse(row["do_not_include_in_total"])
        after = next(
            r for r in stub["rows"] if r["salary_component"] == "Rent Deduction"
        )
        self.assertEqual(Decimal("6.00"), Decimal(after["amount"]))
        self.assertTrue(after["do_not_include_in_total"])
        names = {r["salary_component"] for r in stub["rows"]}
        self.assertEqual({v[0] for v in ish.WAGE_TYPES.values()}, names)
        self.assertEqual(
            2, sum(r["salary_component"] == "Basic Salary" for r in stub["rows"])
        )

    def test_plan_apply_rerun_and_negative_net(self):
        rows = records()
        rows = [dict(r) for r in rows]
        for row in rows:
            if row["wage_type"] == "6500":
                row["amount"] = "-1.00"
            if row["wage_type"] == "6600":
                row["amount"] = "2.00"
            if row["wage_type"] == "5900":
                row["amount"] = "-1001.00"
            if row["wage_type"] == "5010":
                row["amount"] = "-1001.00"
            if row["wage_type"] in ("5020", "5040", "5050"):
                row["amount"] = "0.00"
        erp = FakeErp()
        lookups = ish.Lookups.from_erp(erp)
        plan = ish.plan(rows, lookups)
        self.assertEqual(ish.LOAD, plan[0]["outcome"])
        self.assertEqual("-1.00", plan[0]["payload"]["net_pay"])
        ish.apply(plan, erp)
        self.assertEqual(1, len(erp.calls))
        rerun = ish.plan(rows, ish.Lookups.from_erp(erp))
        self.assertEqual(ish.LOADED, rerun[0]["outcome"])

    def test_each_bad_outcome_is_reported_and_not_loaded(self):
        for mutation, expected in (
            (lambda rs: rs.append({**rs[0], "wage_type": "9999"}), "unknown wage type"),
            (lambda rs: rs[0].update(amount="999.00"), "sums differ"),
        ):
            rs = records()
            mutation(rs)
            p = ish.plan(rs, ish.Lookups({"invented-1": "EMP-TEST"}, {}))
            self.assertEqual(expected, p[0]["outcome"])
        rs = records()
        self.assertEqual("no employee", ish.plan(rs, ish.Lookups({}, {}))[0]["outcome"])
        rs = records()
        for r in rs:
            r["year"], r["month"] = "2027", "1"
        self.assertEqual(
            "after cutover",
            ish.plan(rs, ish.Lookups({"invented-1": "EMP-TEST"}, {}))[0]["outcome"],
        )

    def test_differences_and_check_proposal(self):
        rs = records()
        base = ish.plan(rs, ish.Lookups({"invented-1": "EMP-TEST"}, {}))[0]["payload"]
        slip = {
            "name": "SAL-TEST",
            "swiss_bexio_id": base["bexio_id"],
            "gross_pay": "1",
            "total_deduction": base["total_deduction"],
            "net_pay": base["net_pay"],
            "rounded_total": base["paid_out"],
            "row_count": len(base["earnings"]) + len(base["deductions"]),
        }
        self.assertEqual(
            ish.DIFFERS,
            ish.plan(
                rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: slip})
            )[0]["outcome"],
        )
        slip["gross_pay"] = base["gross_pay"]
        self.assertEqual(
            ish.LOADED,
            ish.plan(
                rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: slip})
            )[0]["outcome"],
        )
        slip["is_proposed"] = 0
        rows = ish.plan(
            rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: slip})
        )
        self.assertEqual(
            1, ish.check_rows(rows, ish.Lookups({}, {base["bexio_id"]: slip}))
        )
        self.assertEqual(ish.DIFFERS, rows[0]["outcome"])

    def test_apply_is_all_or_nothing(self):
        erp = FakeErp()
        bad = records()
        bad[0]["amount"] = "999.00"
        planned = ish.plan(bad, ish.Lookups.from_erp(erp))
        self.assertTrue(any(r["outcome"] in ish.BAD for r in planned))
        self.assertFalse(erp.calls)


if __name__ == "__main__":
    unittest.main()
