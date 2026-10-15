"""Offline tests for swiss-structure-remake.py: no site, no container.

The plan the script follows (create, move, cancel, restore) is read from its
pure parts with frappe mocked; the wrapper's project override is tested the
way test_swiss_setup.py tests swiss-setup.sh.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.modules.setdefault("frappe", mock.MagicMock())
sys.modules.setdefault("frappe.utils", mock.MagicMock())

_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "swiss-structure-remake.py"
)
_spec = importlib.util.spec_from_file_location("swiss_structure_remake", _path)
remake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(remake)


class FakeDoc:
    """The bits of a frappe document the script touches."""

    def __init__(self, doctype, values):
        self.doctype = doctype
        self.__dict__.update(values)
        self.docstatus = values.get("docstatus", 0)

    def get(self, field):
        return self.__dict__.get(field)

    def cancel(self):
        self.docstatus = 2

    def insert(self):
        pass

    def submit(self):
        self.docstatus = 1


class MovePlanTest(unittest.TestCase):
    """move_assignments' decisions, with the frappe calls stubbed."""

    def run_moves(self, old_rows, new_employees):
        created = []

        def fake_get_all(doctype, filters=None, fields=None, pluck=None):
            if doctype == "Salary Structure Assignment" and filters:
                if filters.get("salary_structure") == remake.OLD_NAME:
                    return old_rows
                if filters.get("salary_structure") == "NEW":
                    return list(new_employees)  # pluck returns the employee names
            return []

        def fake_get_doc(*args):
            if isinstance(args[0], str):  # get_doc(doctype, name): read the old row
                row = next(r for r in old_rows if r["name"] == args[1])
                return FakeDoc("Salary Structure Assignment", dict(row))
            doc = FakeDoc("Salary Structure Assignment", dict(args[0]))
            created.append(doc)  # get_doc(dict): the assignment the script makes
            return doc

        with mock.patch.object(remake, "frappe") as frappe:
            frappe.get_all.side_effect = fake_get_all
            frappe.get_doc.side_effect = fake_get_doc
            moved = remake.move_assignments(remake.OLD_NAME, "NEW")
        return moved, created

    def test_moves_a_submitted_old_assignment(self):
        old_rows = [
            {
                "name": "SSA-1",
                "docstatus": 1,
                "employee": "EMP-A",
                "company": "BI Concepts",
                "from_date": "2025-01-01",
                "base": 1,
                "swiss_bvg_insured_salary": 2,
            },
        ]
        moved, created = self.run_moves(old_rows, [])
        self.assertEqual(moved, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].salary_structure, "NEW")
        self.assertEqual(created[0].employee, "EMP-A")
        # payroll_payable_account is deliberately not carried: validate fills it
        self.assertFalse(hasattr(created[0], "payroll_payable_account"))

    def test_skips_an_employee_already_on_the_new_structure(self):
        old_rows = [{"name": "SSA-1", "docstatus": 1, "employee": "EMP-A"}]
        moved, created = self.run_moves(old_rows, ["EMP-A"])
        self.assertEqual(moved, 0)
        self.assertEqual(created, [])

    def test_restores_an_employee_whose_old_assignment_is_cancelled(self):
        # an interrupted run: the old assignment is cancelled, the new one is missing
        old_rows = [{"name": "SSA-1", "docstatus": 2, "employee": "EMP-A"}]
        moved, created = self.run_moves(old_rows, [])
        self.assertEqual(moved, 1)
        self.assertEqual(created[0].employee, "EMP-A")

    def test_two_old_rows_of_one_employee_make_one_assignment(self):
        old_rows = [
            {"name": "SSA-1", "docstatus": 2, "employee": "EMP-A"},
            {"name": "SSA-2", "docstatus": 1, "employee": "EMP-A"},
        ]
        moved, created = self.run_moves(old_rows, [])
        self.assertEqual(moved, 1)
        self.assertEqual(len(created), 1)


class FreeNameTest(unittest.TestCase):
    def test_first_free(self):
        with mock.patch.object(remake.frappe, "get_all", return_value=[]):
            self.assertEqual(remake.free_name(remake.OLD_NAME), remake.OLD_NAME)

    def test_suffix_follows_the_copy_pattern(self):
        with mock.patch.object(
            remake.frappe, "get_all", return_value=[remake.OLD_NAME]
        ):
            self.assertEqual(remake.free_name(remake.OLD_NAME), f"{remake.OLD_NAME} 2")

    def test_skips_taken_suffixes(self):
        with mock.patch.object(
            remake.frappe,
            "get_all",
            return_value=[remake.OLD_NAME, f"{remake.OLD_NAME} 2"],
        ):
            self.assertEqual(remake.free_name(remake.OLD_NAME), f"{remake.OLD_NAME} 3")


class WrapperProjectTest(unittest.TestCase):
    """FINANCE_PROJECT selects the compose project, as in swiss-setup.sh."""

    def run_wrapper(self, project=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            docker = tmp_path / "docker"
            docker.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$DOCKER_ARGS_FILE"\ncat >/dev/null\n',
                encoding="utf-8",
            )
            docker.chmod(0o755)
            args_file = tmp_path / "docker-args"
            env = {
                "PATH": f"{tmp}:{os.environ['PATH']}",
                "DOCKER_ARGS_FILE": str(args_file),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                if project is None:
                    os.environ.pop("FINANCE_PROJECT", None)
                else:
                    os.environ["FINANCE_PROJECT"] = project
                subprocess.run(
                    [
                        "sh",
                        os.path.join(
                            os.path.dirname(_path), "swiss-structure-remake.sh"
                        ),
                    ],
                    check=True,
                    stdin=subprocess.DEVNULL,
                )
            return args_file.read_text(encoding="utf-8").splitlines()

    def test_wrapper_defaults_to_live_project(self):
        args = self.run_wrapper()
        self.assertEqual(args[args.index("-p") + 1], "frappe-finance")

    def test_wrapper_uses_configured_project(self):
        args = self.run_wrapper("frappe-finance-copy")
        self.assertEqual(args[args.index("-p") + 1], "frappe-finance-copy")


if __name__ == "__main__":
    unittest.main()
