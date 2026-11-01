"""Pure validation tests for historical paystub payloads; all values are invented."""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from bi_payroll.salary_history import load_slip, remove_slip, validate_slip


class ValidateSlipTest(unittest.TestCase):
    def payload(self):
        return {
            "bexio_id": "paystub-EMP-TEST-001-2026-10",
            "employee": "EMP-TEST-001",
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "gross_pay": 1000,
            "total_deduction": 0,
            "net_pay": 1000,
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
            lambda name: {
                "Hourly Wage": {"type": "Earning", "do_not_include_in_total": 0},
                "Advance Payment": {"type": "Deduction", "do_not_include_in_total": 1},
                "Rent Deduction": {"type": "Deduction", "do_not_include_in_total": 1},
                "Test Deduction": {"type": "Deduction", "do_not_include_in_total": 0},
                "Expenses Effective": {"type": "Earning", "do_not_include_in_total": 1},
                "Expenses Lump Sum": {"type": "Earning", "do_not_include_in_total": 1},
            }.get(name),
            lambda key: exists,
        )

    def test_accepts_negative_correction_and_duplicate_component_rows(self):
        self.assertEqual(self.validate(self.payload())[1].isoformat(), "2026-10-31")

    def test_accepts_negative_net(self):
        slip = self.payload()
        slip["total_deduction"] = 1200
        slip["net_pay"] = -200
        slip["paid_out"] = -200
        slip["deductions"] = [{"salary_component": "Test Deduction", "amount": 1200}]
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

    def test_component_type_must_match_its_salary_table(self):
        for table, component, amount, gross, deduction in (
            ("earnings", "Advance Payment", 1000, 1000, 0),
            ("deductions", "Hourly Wage", 120, 0, 120),
        ):
            slip = self.payload()
            slip["earnings"] = []
            slip["deductions"] = []
            slip[table] = [{"salary_component": component, "amount": amount}]
            slip["gross_pay"] = gross
            slip["total_deduction"] = deduction
            slip["net_pay"] = gross - deduction
            slip["paid_out"] = gross - deduction
            with self.subTest(table=table), self.assertRaisesRegex(
                ValueError, "must be an"
            ):
                self.validate(slip)

    def test_component_exclusion_default_is_applied_and_explicit_flag_is_kept(self):
        for table, component in (
            ("earnings", "Expenses Effective"),
            ("earnings", "Expenses Lump Sum"),
            ("deductions", "Rent Deduction"),
        ):
            slip = self.payload()
            slip[table].append({"salary_component": component, "amount": 50})
            normalized = self.validate(slip)
            rows = normalized[2] if table == "earnings" else normalized[3]
            self.assertEqual(rows[-1]["do_not_include_in_total"], 1)
            slip[table][-1]["do_not_include_in_total"] = 0
            if table == "earnings":
                slip["gross_pay"] += 50
                slip["net_pay"] += 50
                slip["paid_out"] += 50
            else:
                slip["total_deduction"] += 50
                slip["net_pay"] -= 50
                slip["paid_out"] -= 50
            with self.subTest(table=table):
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
            ("2026-10-02", "2026-10-31"),
            ("2026-10-01", "2026-10-30"),
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


class MalformedSlipTest(unittest.TestCase):
    payload = ValidateSlipTest.payload
    validate = ValidateSlipTest.validate

    def test_all_stored_amounts_must_be_finite_numbers(self):
        for value in ("NaN", "Infinity", "-Infinity", "invalid", None):
            for field in ("gross_pay", "total_deduction", "net_pay", "paid_out"):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ValueError
                ):
                    self.validate({**self.payload(), field: value})
            slip = self.payload()
            slip["earnings"].append(
                {
                    "salary_component": "Hourly Wage",
                    "amount": value,
                    "do_not_include_in_total": 1,
                }
            )
            with self.subTest(row=value), self.assertRaises(ValueError):
                self.validate(slip)

    def test_bad_row_and_exclusion_flag_are_refused(self):
        for row in (
            None,
            "bad",
            {
                "salary_component": "Hourly Wage",
                "amount": 0,
                "do_not_include_in_total": "0",
            },
        ):
            slip = self.payload()
            slip["earnings"].append(row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.validate(slip)


class HistoryMethodsTest(unittest.TestCase):
    def setUp(self):
        self.frappe = mock.MagicMock()
        self.frappe._ = lambda message: message

        def throw(message):
            raise ValueError(message)

        self.frappe.throw.side_effect = throw
        self.frappe.db.exists.side_effect = lambda dt, key: dt != "Salary Slip"
        self.employee = SimpleNamespace(
            name="EMP-TEST-001", employee_name="Test Employee", company="Test Company"
        )
        self.doc = mock.MagicMock(name="history_doc")
        self.doc.name = "TEST-SLIP"
        self.frappe.get_doc.side_effect = lambda *args: (
            self.employee if args[0] == "Employee" else self.doc
        )
        components = {
            "Hourly Wage": {"type": "Earning", "do_not_include_in_total": 0},
            "Advance Payment": {"type": "Deduction", "do_not_include_in_total": 1},
            "Rent Deduction": {"type": "Deduction", "do_not_include_in_total": 1},
            "Expenses Effective": {"type": "Earning", "do_not_include_in_total": 1},
        }
        self.frappe.db.get_value.side_effect = lambda doctype, name, *args, **kwargs: (
            components.get(name) if doctype == "Salary Component" else None
        )
        self.patch = mock.patch.dict(sys.modules, {"frappe": self.frappe})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.slip = ValidateSlipTest().payload()

    def test_snapshot_is_inserted_and_submitted_without_submit_hook(self):
        self.assertEqual(load_slip(self.slip), "TEST-SLIP")
        self.frappe.only_for.assert_called_once_with("System Manager")
        data = self.frappe.get_doc.call_args.args[0]
        for field in ("gross_pay", "total_deduction", "net_pay"):
            self.assertEqual(data[field], self.slip[field])
            self.assertEqual(data["base_" + field], self.slip[field])
        self.assertEqual(data["rounded_total"], self.slip["paid_out"])
        self.assertEqual(data["base_rounded_total"], self.slip["paid_out"])
        self.assertEqual(data["employee_name"], self.employee.employee_name)
        self.assertEqual(data["company"], self.employee.company)
        self.assertEqual(data["posting_date"].isoformat(), self.slip["end_date"])
        self.assertEqual(data["payment_days"], 31)
        self.assertEqual(data["total_working_days"], 31)
        self.assertEqual(data["currency"], "CHF")
        self.assertEqual(data["exchange_rate"], 1)
        self.assertEqual(data["payroll_frequency"], "Monthly")
        self.assertEqual(data["salary_structure"], "")
        self.assertEqual(data["is_proposed"], 1)
        self.assertEqual(data["swiss_bexio_id"], self.slip["bexio_id"])
        self.assertEqual(len(data["earnings"]), len(self.slip["earnings"]))
        self.assertEqual(data["deductions"][0]["do_not_include_in_total"], 1)
        for flag in (
            "ignore_validate",
            "ignore_mandatory",
            "ignore_permissions",
            "ignore_links",
        ):
            self.assertIs(getattr(self.doc.flags, flag), True)
        self.doc.insert.assert_called_once_with()
        self.doc.submit.assert_not_called()
        self.frappe.db.set_value.assert_called_once_with(
            "Salary Slip",
            "TEST-SLIP",
            {"docstatus": 1, "status": "Submitted"},
            update_modified=False,
        )
        name = self.frappe.db.sql.call_args.args[1]
        self.assertEqual(name, "TEST-SLIP")
        self.frappe.db.commit.assert_not_called()

    def test_duplicate_returns_exists_without_writes(self):
        self.frappe.db.exists.return_value = True
        self.frappe.db.exists.side_effect = None
        self.assertEqual(load_slip(self.slip), "exists")
        self.frappe.get_doc.assert_not_called()
        self.frappe.db.set_value.assert_not_called()

    def test_invalid_payload_never_reaches_insert(self):
        for slip in (
            None,
            {**self.slip, "paid_out": "NaN"},
            {**self.slip, "bexio_id": " paystub-test "},
        ):
            with self.subTest(slip=slip), self.assertRaises(ValueError):
                load_slip(slip)
        self.frappe.get_doc.assert_not_called()

    def test_remove_rejects_empty_and_non_history_keys_before_lookup(self):
        for key in (None, "", "ordinary-slip", " paystub-test "):
            with self.subTest(key=key), self.assertRaises(ValueError):
                remove_slip(key)
        self.frappe.db.get_value.assert_not_called()
        self.frappe.delete_doc.assert_not_called()

    def test_remove_looks_up_only_the_history_key_and_cancels_before_delete(self):
        self.frappe.db.get_value.side_effect = lambda *args, **kwargs: "TEST-SLIP"
        self.assertEqual(remove_slip(self.slip["bexio_id"]), "TEST-SLIP")
        self.frappe.only_for.assert_called_once_with("System Manager")
        self.frappe.db.get_value.assert_called_once_with(
            "Salary Slip", {"swiss_bexio_id": self.slip["bexio_id"]}, "name"
        )
        self.doc.cancel.assert_called_once_with()
        for flag in (
            "ignore_validate",
            "ignore_mandatory",
            "ignore_permissions",
            "ignore_links",
        ):
            self.assertIs(getattr(self.doc.flags, flag), True)
        self.frappe.delete_doc.assert_called_once_with(
            "Salary Slip", "TEST-SLIP", ignore_permissions=True, force=True
        )

    def test_remove_missing_history_does_not_delete(self):
        self.frappe.db.get_value.return_value = None
        with self.assertRaisesRegex(ValueError, "not found"):
            remove_slip(self.slip["bexio_id"])
        self.frappe.delete_doc.assert_not_called()

    def test_both_methods_require_system_manager_before_database_access(self):
        self.frappe.only_for.side_effect = PermissionError
        for function, arg in (
            (load_slip, self.slip),
            (remove_slip, self.slip["bexio_id"]),
        ):
            with self.assertRaises(PermissionError):
                function(arg)
        self.frappe.db.exists.assert_not_called()
        self.frappe.db.get_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
