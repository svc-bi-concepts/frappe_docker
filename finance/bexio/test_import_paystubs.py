"""Offline tests for import_paystubs.py. Invented employees, ids and bytes only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import import_files as imf
import import_master as im
import import_paystubs as ips
from test_import_master import FakeErp

EMP_A = "emp-invented-a"
EMP_B = "emp-invented-b"
DATA = b"%PDF-invented"


def record(employee_id, year, month, data=DATA):
    """A paystubs.json row for an invented employee's paystub, and the file name the export gives it."""
    return {"employee_id": employee_id, "year": year, "month": month, "bytes": len(data),
            "file": "{}-{}-{:02d}.pdf".format(employee_id, year, month)}


def write_export(root, records=None, contents=None):
    """An export: paystubs.json (None for no file) and the PDFs under paystubs/, {file: bytes}."""
    if records is not None:
        with open(os.path.join(root, "paystubs.json"), "w", encoding="utf-8") as f:
            json.dump(records, f)
    os.makedirs(os.path.join(root, "paystubs"), exist_ok=True)
    for name, data in (contents or {}).items():
        with open(os.path.join(root, "paystubs", name), "wb") as f:
            f.write(data)


def erp_with(employees=(), files=()):
    return FakeErp({"Employee": list(employees),
                    "File": [dict({"name": "FILE-{}".format(i)}, **f) for i, f in enumerate(files, 1)]})


def employee(name, bexio_id):
    return {"name": name, "bexio_id": bexio_id}


class KeyTest(unittest.TestCase):
    def test_the_key_is_the_employee_year_and_month_and_starts_with_paystub(self):
        self.assertEqual(ips.file_key({"employee_id": EMP_A, "year": 2024, "month": 3}), "paystub-emp-invented-a-2024-03")

    def test_the_file_name_has_the_year_and_month_and_no_person(self):
        self.assertEqual(ips.file_name({"employee_id": EMP_A, "year": 2024, "month": 3}), "2024-03-paystub.pdf")
        self.assertNotIn(EMP_A, ips.file_name({"employee_id": EMP_A, "year": 2024, "month": 3}))

    def test_the_key_of_each_month_is_different(self):
        keys = {ips.file_key(record(EMP_A, 2024, m)) for m in range(1, 13)}
        self.assertEqual(len(keys), 12)


class LookupsTest(unittest.TestCase):
    def test_employees_are_keyed_by_bexio_id_and_only_paystub_keys_are_read(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A)],
                       files=[{"bexio_id": "paystub-emp-invented-a-2024-03"}, {"bexio_id": "12"},
                              {"bexio_id": "credit-4"}, {"bexio_id": None}])
        lookups = ips.Lookups.from_erp(erp)
        self.assertEqual(lookups.employees, {EMP_A: "HR-EMP-0001"})
        self.assertEqual(lookups.keys, {"paystub-emp-invented-a-2024-03"})


class PlanTest(unittest.TestCase):
    def plan(self, records, employees=(), files=(), contents=None):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, contents=contents)
            lookups = ips.Lookups.from_erp(erp_with(employees, files))
            return ips.plan(records, lookups, os.path.join(root, "paystubs"))

    def test_a_paystub_of_a_found_employee_is_to_attach(self):
        rows = self.plan([record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA})
        self.assertEqual(rows[0]["outcome"], ips.ATTACH)
        self.assertEqual((rows[0]["employee"], rows[0]["key"], rows[0]["name"]),
                         ("HR-EMP-0001", "paystub-emp-invented-a-2024-03", "2024-03-paystub.pdf"))

    def test_a_paystub_whose_key_is_on_a_file_is_attached(self):
        rows = self.plan([record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         files=[{"bexio_id": "paystub-emp-invented-a-2024-03"}],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA})
        self.assertEqual(rows[0]["outcome"], ips.ATTACHED)

    def test_a_paystub_whose_employee_is_missing_is_counted_not_attached(self):
        rows = self.plan([record(EMP_B, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         contents={"{}-2024-03.pdf".format(EMP_B): DATA})
        self.assertEqual(rows[0]["outcome"], ips.NO_EMPLOYEE)
        self.assertIsNone(rows[0]["employee"])

    def test_a_pdf_missing_on_disk_is_a_problem(self):
        rows = self.plan([record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)])
        self.assertEqual(rows[0]["outcome"], "no content")

    def test_a_pdf_of_another_size_than_the_export_is_a_problem(self):
        rows = self.plan([record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA + b"-more"})
        self.assertEqual(rows[0]["outcome"], "size differs")

    def test_the_same_employee_and_month_twice_is_a_problem_not_two_uploads(self):
        rows = self.plan([record(EMP_A, 2024, 3), record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA})
        self.assertEqual([r["outcome"] for r in rows], [ips.ATTACH, "duplicate"])

    def test_the_employee_id_is_matched_as_text(self):
        rows = self.plan([record(EMP_A, 2024, 3)], employees=[employee("HR-EMP-0001", EMP_A)],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA})
        self.assertEqual(rows[0]["employee"], "HR-EMP-0001")


class ApplyTest(unittest.TestCase):
    def test_apply_uploads_each_pdf_to_attach_on_its_employee(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A), employee("HR-EMP-0002", EMP_B)])
        records = [record(EMP_A, 2024, 3), record(EMP_B, 2024, 3), record("emp-invented-x", 2024, 3)]
        with tempfile.TemporaryDirectory() as root:
            write_export(root, records=records, contents={r["file"]: DATA for r in records})
            rows, _ = ips.run(records, erp, os.path.join(root, "paystubs"))
            calls = []
            with mock.patch.object(imf, "upload", lambda erp, doc, key, name, content, doctype: calls.append((doctype, doc, key, name, content))):
                uploaded = ips.apply(rows, os.path.join(root, "paystubs"), erp)
        self.assertEqual(uploaded, 2)
        self.assertEqual(calls, [("Employee", "HR-EMP-0001", "paystub-emp-invented-a-2024-03", "2024-03-paystub.pdf", DATA),
                                 ("Employee", "HR-EMP-0002", "paystub-emp-invented-b-2024-03", "2024-03-paystub.pdf", DATA)])

    def test_a_second_run_finds_the_files_the_first_one_attached(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A)])
        records = [record(EMP_A, 2024, 3), record(EMP_A, 2024, 4)]
        with tempfile.TemporaryDirectory() as root:
            paystubs = os.path.join(root, "paystubs")
            write_export(root, records=records, contents={r["file"]: DATA for r in records})
            rows, _ = ips.run(records, erp, paystubs)
            uploaded_keys = []

            def fake_upload(erp, doc, key, name, content, doctype):
                uploaded_keys.append(key)
                erp.docs.setdefault("File", {})[name] = {"name": name, "bexio_id": key, "attached_to_name": doc}

            with mock.patch.object(imf, "upload", fake_upload):
                self.assertEqual(ips.apply(rows, paystubs, erp), 2)
            rows, _ = ips.run(records, erp, paystubs)
            self.assertEqual(ips.apply(rows, paystubs, erp), 0)
        self.assertEqual(len(uploaded_keys), len(set(uploaded_keys)))
        self.assertEqual([r["outcome"] for r in rows], [ips.ATTACHED, ips.ATTACHED])


class ReportTest(unittest.TestCase):
    def test_the_report_has_counts_and_no_ids_or_names(self):
        rows = [
            {"employee_id": EMP_A, "employee": "HR-EMP-0001", "outcome": ips.ATTACH},
            {"employee_id": EMP_A, "employee": "HR-EMP-0001", "outcome": ips.ATTACHED},
            {"employee_id": EMP_B, "employee": None, "outcome": ips.NO_EMPLOYEE},
        ]
        text = ips.report(rows)
        self.assertIn("3 record(s)", text)
        self.assertIn("2 in the export, 1 found in ERPNext, 1 missing", text)
        self.assertNotIn(EMP_A, text)
        self.assertNotIn("HR-EMP", text)
        self.assertIn("dry run: nothing was written", text)


class MainTest(unittest.TestCase):
    def test_a_missing_paystubs_json_aborts_with_status_2(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root)
            with mock.patch.object(im.Erp, "from_file", return_value=erp_with()):
                self.assertEqual(ips.main(["--paystubs-json", os.path.join(root, "paystubs.json")]), 2)

    def test_a_dry_run_writes_nothing_to_erpnext(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A)])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, records=[record(EMP_A, 2024, 3)], contents={"{}-2024-03.pdf".format(EMP_A): DATA})
            with mock.patch.object(im.Erp, "from_file", return_value=erp), \
                    mock.patch.object(imf, "upload") as upload, mock.patch.object(ips.ip, "write_private") as write:
                self.assertEqual(ips.main(["--paystubs-json", os.path.join(root, "paystubs.json")]), 0)
        upload.assert_not_called()
        write.assert_not_called()

    def test_an_apply_run_uploads_and_a_missing_employee_is_counted_not_uploaded(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A)])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, records=[record(EMP_A, 2024, 3), record(EMP_B, 2024, 3)],
                         contents={"{}-2024-03.pdf".format(EMP_A): DATA, "{}-2024-03.pdf".format(EMP_B): DATA})
            with mock.patch.object(im.Erp, "from_file", return_value=erp), \
                    mock.patch.object(imf, "upload") as upload, mock.patch.object(ips.ip, "write_private") as write:
                self.assertEqual(ips.main(["--paystubs-json", os.path.join(root, "paystubs.json"), "--apply"]), 0)
        upload.assert_called_once()
        self.assertEqual(write.call_args[0][1], ["paystub record 2: no employee"])

    def test_a_problem_makes_the_run_fail_and_lists_its_record_position(self):
        erp = erp_with(employees=[employee("HR-EMP-0001", EMP_A)])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, records=[record(EMP_A, 2024, 3)])
            with mock.patch.object(im.Erp, "from_file", return_value=erp), \
                    mock.patch.object(ips.ip, "write_private") as write:
                self.assertEqual(ips.main(["--paystubs-json", os.path.join(root, "paystubs.json")]), 1)
        self.assertEqual(write.call_args[0][1], ["paystub record 1: no content"])


if __name__ == "__main__":
    unittest.main()
