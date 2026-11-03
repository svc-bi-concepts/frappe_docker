"""Offline salary-history importer tests with invented paystub figures only."""

import contextlib
import csv
import http.client
import io
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

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
            raise AssertionError("Child table lists lack parent permission context")
        return []

    def get(self, doctype, name):
        assert doctype == "Salary Slip"
        return next(s for s in self.slips if s["name"] == name)

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "bi_payroll.salary_history.remove_slip":
            self.slips = [
                s for s in self.slips if s["swiss_bexio_id"] != kwargs["bexio_id"]
            ]
            return
        assert method == "bi_payroll.salary_history.load_slip"
        p = kwargs["slip"]
        self.row_count = sum(len(p[k]) for k in ("earnings", "deductions"))
        self.slips.append(
            {
                "name": f"SAL-TEST-{len(self.slips)}",
                "earnings": p["earnings"],
                "deductions": p["deductions"],
                "swiss_bexio_id": p["bexio_id"],
                "gross_pay": p["gross_pay"],
                "total_deduction": p["total_deduction"],
                "net_pay": p["net_pay"],
                "rounded_total": p["paid_out"],
                "is_proposed": 1,
                "employee": p["employee"],
                "start_date": p["start_date"],
                "end_date": p["end_date"],
                "docstatus": 1,
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
            "earnings": base["earnings"],
            "deductions": base["deductions"],
            "is_proposed": 1,
            "employee": base["employee"],
            "start_date": base["start_date"],
            "end_date": base["end_date"],
            "docstatus": 1,
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
        for field, value in (
            ("employee", "EMP-OTHER"),
            ("start_date", "2026-09-01"),
            ("end_date", "2026-09-30"),
            ("docstatus", 0),
        ):
            changed_identity = dict(slip, **{field: value})
            self.assertEqual(
                ish.DIFFERS,
                ish.plan(
                    rs,
                    ish.Lookups(
                        {"invented-1": "EMP-TEST"},
                        {base["bexio_id"]: changed_identity},
                    ),
                )[0]["outcome"],
            )
        changed = dict(slip)
        changed["earnings"] = [dict(row) for row in slip["earnings"]]
        changed["earnings"][0]["amount"] = "899.00"
        lookups = ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: changed})
        self.assertEqual(ish.DIFFERS, ish.plan(rs, lookups)[0]["outcome"])
        changed["earnings"] = [dict(row) for row in slip["earnings"]]
        changed["earnings"][0]["do_not_include_in_total"] = 1
        self.assertEqual(
            ish.DIFFERS,
            ish.plan(
                rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: changed})
            )[0]["outcome"],
        )
        changed["earnings"] = [dict(row) for row in slip["earnings"]]
        changed["deductions"] = [dict(row) for row in slip["deductions"]]
        changed["deductions"][0]["salary_component"] = "Different Component"
        self.assertEqual(
            ish.DIFFERS,
            ish.plan(
                rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: changed})
            )[0]["outcome"],
        )
        slip["is_proposed"] = 0
        rows = ish.plan(
            rs, ish.Lookups({"invented-1": "EMP-TEST"}, {base["bexio_id"]: slip})
        )
        self.assertEqual(1, ish.check_rows(ish.Lookups({}, {base["bexio_id"]: slip})))
        self.assertEqual(ish.DIFFERS, rows[0]["outcome"])

    def run_command(self, erp, rows=None, args=()):
        with tempfile.TemporaryDirectory() as folder:
            figures = os.path.join(folder, "figures.csv")
            with open(figures, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=records()[0].keys())
                writer.writeheader()
                writer.writerows(records() if rows is None else rows)
            output, errors = io.StringIO(), io.StringIO()
            problems = os.path.join(folder, "payroll", "problems.txt")
            with (
                patch.object(ish.im.Erp, "from_file", return_value=erp),
                patch.object(ish, "PROBLEMS_FILE", problems),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                result = ish.main(["--figures", figures, *args])
            private_report = ""
            if os.path.exists(problems):
                self.assertEqual(0o600, os.stat(problems).st_mode & 0o777)
                with open(problems, encoding="utf-8") as stream:
                    private_report = stream.read()
            self.assertNotIn("invented-1", output.getvalue() + errors.getvalue())
            return result, output.getvalue(), errors.getvalue(), private_report

    def test_apply_is_all_or_nothing(self):
        for mutation in ("unknown", "sum", "employee", "cutover", "differs"):
            with self.subTest(mutation=mutation):
                erp = FakeErp()
                bad = [{**r, "month": "11"} for r in records()]
                if mutation == "unknown":
                    bad.append({**bad[0], "wage_type": "9999"})
                elif mutation == "sum":
                    bad[0]["amount"] = "999.00"
                elif mutation == "employee":
                    bad = [{**r, "employee_id": "invented-missing"} for r in bad]
                elif mutation == "cutover":
                    bad = [{**r, "year": "2027"} for r in bad]
                else:
                    ish.apply(ish.plan(bad, ish.Lookups.from_erp(erp)), erp)
                    erp.slips[0]["net_pay"] = "1"
                    erp.calls.clear()
                result, output, _, report = self.run_command(
                    erp, records() + bad, ["--apply"]
                )
                self.assertEqual(1, result)
                self.assertIn("apply refused", output)
                self.assertTrue(report.strip())
                self.assertEqual([], erp.calls)

    def test_cli_dry_run_apply_rerun_and_check(self):
        erp = FakeErp()
        self.assertEqual(0, self.run_command(erp)[0])
        self.assertEqual([], erp.calls)
        self.assertEqual(1, self.run_command(erp, args=["--check"])[0])
        self.assertEqual([], erp.calls)
        self.assertEqual(0, self.run_command(erp, args=["--apply"])[0])
        self.assertEqual(1, len(erp.calls))
        self.assertEqual(0, self.run_command(erp, args=["--apply"])[0])
        self.assertEqual(1, len(erp.calls))
        self.assertEqual(0, self.run_command(erp, args=["--check"])[0])
        for field in (
            "gross_pay",
            "total_deduction",
            "net_pay",
            "rounded_total",
            "is_proposed",
        ):
            with self.subTest(field=field):
                value = erp.slips[0][field]
                erp.slips[0][field] = 0
                self.assertEqual(1, self.run_command(erp, args=["--check"])[0])
                erp.slips[0][field] = value
        erp.slips[0]["is_proposed"] = 0
        erp.calls.clear()
        result, output, _, _ = self.run_command(erp, args=["--apply"])
        self.assertEqual(1, result)
        self.assertIn("apply refused", output)
        self.assertEqual([], erp.calls)
        erp.slips[0]["is_proposed"] = 1
        erp.slips[0]["earnings"].pop()
        self.assertEqual(1, self.run_command(erp, args=["--check"])[0])

    def test_empty_figures_refused_for_apply_and_check(self):
        for args in (["--apply"], ["--check"]):
            with self.subTest(args=args):
                erp = FakeErp()
                result, output, _, _ = self.run_command(erp, rows=[], args=args)
                self.assertEqual(1, result)
                self.assertIn("no stubs in figures CSV", output)
                self.assertEqual([], erp.calls)

    def test_proposal_check_includes_slips_outside_csv(self):
        erp = FakeErp()
        self.run_command(erp, args=["--apply"])
        erp.slips.append(
            {
                **erp.slips[0],
                "name": "SAL-OTHER",
                "swiss_bexio_id": "other-key",
                "is_proposed": 0,
            }
        )
        result, output, _, _ = self.run_command(erp, args=["--check"])
        self.assertEqual(1, result)
        self.assertIn("is_proposed not set: 1", output)

    def test_apply_fails_if_post_write_verification_still_finds_load(self):
        erp = FakeErp()
        with patch.object(erp, "call", return_value="exists"):
            self.assertEqual(1, self.run_command(erp, args=["--apply"])[0])

    def test_zero_gross_negative_net_and_nonfinite_input(self):
        rows = records()
        for row in rows:
            if row["wage_type"] in ("1000", "1001", "1005", "1160", "1218", "5000"):
                row["amount"] = "0.00"
            elif row["wage_type"] == "6500":
                row["amount"] = "-100.00"
            elif row["wage_type"] == "6600":
                row["amount"] = "-97.00"
        erp = FakeErp()
        self.assertEqual(0, self.run_command(erp, rows, ["--apply"])[0])
        self.assertEqual("0.00", erp.slips[0]["gross_pay"])
        self.assertEqual("-100.00", erp.slips[0]["net_pay"])
        for value in ("NaN", "Infinity", "invented-invalid-number"):
            rows[0]["amount"] = value
            erp = FakeErp()
            self.assertEqual(2, self.run_command(erp, rows, ["--apply"])[0])
            self.assertEqual([], erp.calls)

    def test_oldest_first_and_repeated_wage_type_corrections(self):
        erp = FakeErp()
        rows = records()
        rows.extend([{**rows[0], "amount": "-1.00"}, {**rows[0], "amount": "1.00"}])
        later = [{**r, "month": "11"} for r in rows]
        self.assertEqual(0, self.run_command(erp, later + rows, ["--apply"])[0])
        self.assertEqual(
            ["2026-10-01", "2026-11-01"],
            [args["slip"]["start_date"] for _, args in erp.calls],
        )
        self.assertEqual(
            4,
            sum(
                r["salary_component"] == "Basic Salary"
                for r in erp.calls[0][1]["slip"]["earnings"]
            ),
        )

    def test_positive_printed_tax_becomes_negative_deduction(self):
        rows = records()
        for row in rows:
            if row["wage_type"] == "5060":
                row["amount"] = "10.00"
            elif row["wage_type"] == "5900":
                row["amount"] = "-90.00"
            elif row["wage_type"] == "6500":
                row["amount"] = "910.00"
            elif row["wage_type"] == "6600":
                row["amount"] = "913.00"
        plan = ish.plan(rows, ish.Lookups.from_erp(FakeErp()))
        self.assertEqual(ish.LOAD, plan[0]["outcome"])
        tax = next(
            r
            for r in plan[0]["payload"]["deductions"]
            if r["salary_component"] == "Quellensteuer Employee"
        )
        self.assertEqual("-10.00", tax["amount"])

    def test_each_total_equation_and_missing_or_repeated_totals(self):
        for wage_type in ("5000", "5900", "6500", "6600"):
            for mutation in ("change", "missing", "duplicate"):
                with self.subTest(wage_type=wage_type, mutation=mutation):
                    rows = records()
                    total = next(r for r in rows if r["wage_type"] == wage_type)
                    if mutation == "change":
                        total["amount"] = "1.00"
                    elif mutation == "missing":
                        rows.remove(total)
                    else:
                        rows.append(dict(total))
                    plan = ish.plan(rows, ish.Lookups.from_erp(FakeErp()))
                    self.assertEqual("sums differ", plan[0]["outcome"])

    def test_remove_month_preview_and_apply_only_history_keys(self):
        erp = FakeErp()
        self.run_command(erp, args=["--apply"])
        for key in ("", "ordinary-2026-10", "paystub-invented-2-2026-11"):
            erp.slips.append({"name": "SAL-OTHER", "swiss_bexio_id": key})
        erp.calls.clear()
        self.assertEqual(
            0, self.run_command(erp, args=["--remove-month", "2026-10"])[0]
        )
        self.assertEqual([], erp.calls)
        self.assertEqual(
            0, self.run_command(erp, args=["--remove-month", "2026-10", "--apply"])[0]
        )
        self.assertEqual(
            [
                (
                    "bi_payroll.salary_history.remove_slip",
                    {"bexio_id": "paystub-invented-1-2026-10"},
                )
            ],
            erp.calls,
        )
        self.assertEqual(3, len(erp.slips))

    def test_error_messages_do_not_disclose_server_or_file_details(self):
        for args in ([], ["--remove-month", "2026-10", "--apply"]):
            erp = FakeErp()
            with patch.object(
                erp, "list", side_effect=ish.im.ErpError(500, "invented-private-detail")
            ):
                result, _, errors, _ = self.run_command(erp, args=args)
            self.assertEqual(2, result)
            self.assertNotIn("invented-private-detail", errors)
        self.assertEqual(
            2,
            self.run_command(FakeErp(), args=["--figures", "/invented-missing-file"])[
                0
            ],
        )

    def test_a_malformed_status_line_or_a_bad_shape_prints_none_of_it(self):
        for failure in (
            http.client.BadStatusLine("invented-private-detail"),
            AttributeError("invented-private-detail"),
        ):
            for args in ([], ["--remove-month", "2026-10", "--apply"]):
                erp = FakeErp()
                with patch.object(erp, "list", side_effect=failure):
                    result, output, errors, _ = self.run_command(erp, args=args)
                self.assertEqual(2, result)
                self.assertNotIn("invented-private-detail", output + errors)
                self.assertNotIn("Traceback", output + errors)

    def test_report_redacts_unknown_csv_text_and_repairs_permissions(self):
        rows = records() + [{**records()[0], "wage_type": "invented-sensitive-text"}]
        result, output, _, report = self.run_command(FakeErp(), rows)
        self.assertEqual(1, result)
        self.assertIn("unknown wage type", report)
        self.assertNotIn("invented-sensitive-text", report + output)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.txt")
            with open(path, "w") as stream:
                stream.write("stale")
            os.chmod(path, 0o644)
            ish.write_problems(path, [])
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)

    def test_invalid_remove_options_are_refused(self):
        for args in (
            ["--check", "--remove-month", "2026-10"],
            ["--remove-month", "2026-1"],
        ):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit
            ):
                ish.main(args)


if __name__ == "__main__":
    unittest.main()
