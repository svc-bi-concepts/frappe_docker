"""The hrms accrual filter that the history rule rests on: a Payroll Entry accrual picks only slips that name it and have no journal_entry.

finance/docs/hrms.md, 'History: in the GL, no Salary Slips', keeps the payroll history out of Salary Slips: none is loaded
for it. The filter still matters for any Salary Slip without a payroll_entry (made by hand or by a later load), which the
Payroll Entry accrual must never book again. That holds only while get_sal_slip_list filters on payroll_entry and on an
empty journal_entry. This test reads the installed hrms (v16.50.0 in the image) and fails when either condition is gone,
so an hrms bump that loosens the filter stops the gate instead of double-booking payroll. It lives in bi_finance, not in
bi_payroll, because only the frappe-image step of .yardr/check imports hrms; the payroll assumption it guards is bi_payroll's.

Needs the frappe image; no site, no database. The query is built with frappe's own query builder and captured before it runs:

    python3 -m unittest bi_finance.test_hrms_accrual_filter

from apps/bi_finance in the image, as .yardr/check runs it. Invented names only.
"""

import types
import unittest
from unittest import mock

import frappe
from frappe.query_builder import get_query_builder
from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry
from pypika.queries import QueryBuilder

ENTRY = "PE-TEST-0001"
HINT = "hrms changed the accrual filter, re-check hrms.md 'History' before any history load"


def accrual_sql(ss_status=1):
    """The SQL get_sal_slip_list builds for an invented Payroll Entry; the query is captured, not run."""
    entry = types.SimpleNamespace(
        name=ENTRY,
        start_date="2026-01-01",
        end_date="2026-01-31",
        salary_slip_based_on_timesheet=0,
    )
    captured = []

    def run(self, *args, **kwargs):
        captured.append(self.get_sql())
        return []

    with (
        mock.patch.object(frappe, "qb", get_query_builder("mariadb")),
        mock.patch.object(QueryBuilder, "run", run),
    ):
        PayrollEntry.get_sal_slip_list(entry, ss_status=ss_status)
    return captured[0]


class TestAccrualFilter(unittest.TestCase):
    def setUp(self):
        self.sql = accrual_sql()

    def test_slips_must_name_the_payroll_entry(self):
        self.assertRegex(self.sql, rf"`payroll_entry`\s*=\s*'{ENTRY}'", msg=HINT)

    def test_slips_with_a_journal_entry_are_left_out(self):
        self.assertRegex(
            self.sql,
            r"`journal_entry`\s+IS NULL\s+OR\s+`journal_entry`\s*=\s*''",
            msg=HINT,
        )


if __name__ == "__main__":
    unittest.main()
