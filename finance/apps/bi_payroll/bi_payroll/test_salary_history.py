"""Pure validation tests for historical paystub payloads; all values are invented."""

import unittest

from bi_payroll.salary_history import validate_slip


class ValidateSlipTest(unittest.TestCase):
    def payload(self):
        return {
            "bexio_id": "paystub-EMP-TEST-001-2026-10",
            "employee": "EMP-TEST-001",
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "gross_pay": 1000,
            "total_deduction": 120,
            "net_pay": 880,
            "paid_out": 880,
            "earnings": [
                {"salary_component": "Hourly Wage", "amount": 1100},
                {"salary_component": "Hourly Wage", "amount": -100},
            ],
            "deductions": [{"salary_component": "Advance Payment", "amount": 120}],
        }

    def validate(self, slip, exists=False):
        return validate_slip(
            slip,
            lambda name: name == "EMP-TEST-001",
            lambda name: name in {"Hourly Wage", "Advance Payment"},
            lambda key: exists,
        )

    def test_accepts_negative_correction_and_duplicate_component_rows(self):
        self.assertEqual(self.validate(self.payload())[1].isoformat(), "2026-10-31")

    def test_accepts_negative_net(self):
        slip = self.payload()
        slip["total_deduction"] = 1200
        slip["net_pay"] = -200
        slip["paid_out"] = -200
        slip["deductions"] = [{"salary_component": "Advance Payment", "amount": 1200}]
        self.assertEqual(self.validate(slip)[1].isoformat(), "2026-10-31")

    def test_ignored_rows_do_not_change_totals(self):
        slip = self.payload()
        slip["earnings"].append(
            {
                "salary_component": "Hourly Wage",
                "amount": 500,
                "do_not_include_in_total": 1,
            }
        )
        self.assertEqual(self.validate(slip)[1].isoformat(), "2026-10-31")

    def test_refuses_missing_or_malformed_key(self):
        for key in ("", "wrong-key"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "bexio_id"):
                self.validate({**self.payload(), "bexio_id": key})

    def test_refuses_existing_key(self):
        with self.assertRaisesRegex(ValueError, "exists"):
            self.validate(self.payload(), exists=True)

    def test_refuses_unknown_employee_and_component(self):
        with self.assertRaisesRegex(ValueError, "Employee"):
            self.validate({**self.payload(), "employee": "UNKNOWN"})
        slip = self.payload()
        slip["earnings"][0]["salary_component"] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "Salary Component"):
            self.validate(slip)

    def test_refuses_period_outside_one_month_or_after_2026(self):
        for start, end in (
            ("2026-10-01", "2026-11-01"),
            ("2027-01-01", "2027-01-31"),
        ):
            slip = {**self.payload(), "start_date": start, "end_date": end}
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.validate(slip)

    def test_refuses_each_total_mismatch(self):
        for field, value in (
            ("gross_pay", 999),
            ("total_deduction", 121),
            ("net_pay", 879),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "totals"
            ):
                self.validate({**self.payload(), field: value})


if __name__ == "__main__":
    unittest.main()
