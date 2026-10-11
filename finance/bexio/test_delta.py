"""Offline tests for delta.sh: the argument handling, the refusals and the order of the chain.

No ERPNext, no docker, no network: python3 and docker are stubs on PATH, private/ and the
export directory are temporary, and the stub prints canned output so the count extractors and
the rerun gates run for real. Run with: cd finance/bexio && python3 -m unittest test_delta
"""

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "bexio", "delta.sh")

PYTHON_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$DELTA_LOG"
prev=""
for a in "$@"; do
  if [ "$prev" = "--write" ]; then printf '{}' >"$a"; fi
  prev=$a
done
case "$*" in
*delta_counts.py*)
  if [ -n "${DELTA_COUNTS_SEQ_DIR:-}" ]; then
    n=$(ls "$DELTA_COUNTS_SEQ_DIR" 2>/dev/null | grep -c '^call-' || true)
    n=$((n + 1))
    : >"$DELTA_COUNTS_SEQ_DIR/call-$n"
    if [ -f "$DELTA_COUNTS_SEQ_DIR/$n.txt" ]; then cat "$DELTA_COUNTS_SEQ_DIR/$n.txt"; fi
  fi
  exit 0
  ;;
esac
if [ -f "${DELTA_OUT:-/nonexistent}" ]; then cat "$DELTA_OUT"; fi
exit ${DELTA_RC:-0}
"""

DOCKER_STUB = """#!/bin/sh
echo "docker $*" >>"$DELTA_DOCKER_LOG"
exit 0
"""

MASTER_TABLE = """doctype           created  updated unchanged  failed  skipped
Currency                0        0         8       0        0
Account                 0        1       198       0        0
dry run: nothing was written
"""


def signatures(log_path):
    """(basename of the script, its import-relevant flags) per stub call, in order."""
    out = []
    with open(log_path) as f:
        for line in f:
            parts = line.split()
            flags = tuple(p for p in parts[1:] if p in ("--dry-run", "--apply", "--write", "--check"))
            out.append((os.path.basename(parts[0]), flags))
    return out


def table_rows(stdout):
    """The counts table's rows, label -> [new, changed, skipped, failed, exit]."""
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("counts per step"))
    rows = {}
    for line in lines[start + 2 :]:
        parts = line.split()
        if len(parts) >= 5:
            rows[" ".join(parts[0 : len(parts) - 5])] = parts[-5:]
    return rows


DRY_P1 = [
    ("import_master.py", ("--dry-run",)),
    ("import_sales.py", ("--dry-run",)),
    ("import_credit_note.py", ("--write",)),
    ("import_purchase.py", ("--dry-run",)),
    ("import_payments_in.py", ("--dry-run",)),
    ("import_payments_out.py", ("--dry-run",)),
    ("import_vat_fix.py", ("--dry-run",)),
    ("import_bank.py", ("--dry-run",)),
    ("import_manual_entries.py", ("--dry-run",)),
    ("import_manual_fix.py", ("--dry-run",)),
    ("import_payroll.py", ("--dry-run",)),
    ("import_opening.py", ("--dry-run",)),
    ("import_files.py", ("--dry-run",)),
    ("import_document_pdfs.py", ("--dry-run",)),
]


class DeltaScript(unittest.TestCase):
    def run_delta(self, args=None, env_extra=None):
        """Run delta.sh in a scratch layout with stub python3 and docker; return the run."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = os.path.join(tmp.name, "bin")
        priv = os.path.join(tmp.name, "private")
        exp = os.path.join(tmp.name, "export")
        os.makedirs(bin_dir)
        os.makedirs(priv)
        os.makedirs(exp)
        for name, body in (("python3", PYTHON_STUB), ("docker", DOCKER_STUB)):
            path = os.path.join(bin_dir, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        with open(os.path.join(exp, "accounts.json"), "w") as f:
            f.write("[]")
        # the sales and purchase draft plans the apply pass submits, written by the importers
        for name in ("bexio-sales-drafts.json", "bexio-purchase-drafts.json"):
            with open(os.path.join(priv, name), "w") as f:
                f.write("{}")
        log = os.path.join(tmp.name, "python.log")
        dlog = os.path.join(tmp.name, "docker.log")
        env = dict(os.environ)
        env.update(
            {
                "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                "PRIVATE": priv,
                "DELTA_LOG": log,
                "DELTA_DOCKER_LOG": dlog,
            }
        )
        env.pop("DELTA_APPLY", None)
        env.pop("BACKUP_ID", None)
        env.update(env_extra or {})
        run = subprocess.run(
            ["sh", SCRIPT] + (args if args is not None else [exp]), capture_output=True, text=True, env=env
        )
        run.priv = priv
        run.exp = exp
        run.log = log
        run.dlog = dlog
        return run

    # ---- argument handling -------------------------------------------------------

    def test_help(self):
        run = self.run_delta(args=["--help"])
        self.assertEqual(run.returncode, 0)
        self.assertIn("DELTA_APPLY", run.stdout)
        self.assertIn("BACKUP_ID", run.stdout)
        self.assertFalse(os.path.exists(run.log))

    def test_no_args(self):
        run = self.run_delta(args=[])
        self.assertEqual(run.returncode, 2)
        self.assertIn("usage:", run.stderr)

    def test_unknown_option(self):
        run = self.run_delta(args=["--nightly"])
        self.assertEqual(run.returncode, 2)
        self.assertIn("unknown option", run.stderr)

    def test_export_not_a_directory(self):
        run = self.run_delta(args=["/nonexistent/export"])
        self.assertEqual(run.returncode, 2)
        self.assertIn("not a directory", run.stderr)

    def test_apply_needs_backup_id(self):
        run = self.run_delta(env_extra={"DELTA_APPLY": "1"})
        self.assertEqual(run.returncode, 2)
        self.assertIn("BACKUP_ID", run.stderr)
        self.assertFalse(os.path.exists(run.log), "no step may run without the backup id")

    def test_apply_value_is_0_or_1(self):
        run = self.run_delta(env_extra={"DELTA_APPLY": "yes", "BACKUP_ID": "b1"})
        self.assertEqual(run.returncode, 2)
        self.assertIn("0 or 1", run.stderr)

    # ---- the lock ----------------------------------------------------------------

    def test_refuses_while_another_delta_holds_the_lock(self):
        holder = subprocess.Popen(["sleep", "5"])
        self.addCleanup(holder.kill)
        self.addCleanup(holder.wait)
        run = self.run_delta()
        lock = os.path.join(run.priv, ".bexio-delta.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write("{} held".format(holder.pid))
        run2 = self.run_delta(env_extra={"PRIVATE": run.priv})
        self.assertEqual(run2.returncode, 2)
        self.assertIn("another delta run", run2.stderr)
        self.assertFalse(os.path.exists(run2.log), "no step may run under a held lock")

    def test_stale_lock_is_taken_over(self):
        gone = subprocess.Popen(["sleep", "0"])
        gone.wait()
        run = self.run_delta()
        lock = os.path.join(run.priv, ".bexio-delta.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write("{} held".format(gone.pid))
        run2 = self.run_delta(env_extra={"PRIVATE": run.priv})
        self.assertEqual(run2.returncode, 0, run2.stderr)
        self.assertIn("stale", run2.stderr)
        self.assertFalse(os.path.exists(lock), "the lock must be released after the run")

    # ---- the dry pass ------------------------------------------------------------

    def test_dry_run_order(self):
        run = self.run_delta()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            signatures(run.log),
            [("delta_counts.py", ())] + DRY_P1 + [("delta_counts.py", ()), ("check_trial_balance.py", ())],
        )
        self.assertFalse(
            os.path.exists(run.dlog) and open(run.dlog).read(), "the dry run submits nothing"
        )

    def test_dry_run_refuses_moved_counts(self):
        seq = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", seq])
        for i, count in enumerate(("Sales Invoice\t10", "Sales Invoice\t11"), start=1):
            with open(os.path.join(seq, "{}.txt".format(i)), "w") as f:
                f.write(count)
        run = self.run_delta(env_extra={"DELTA_COUNTS_SEQ_DIR": seq})
        self.assertEqual(run.returncode, 5)
        self.assertIn("dry pass moved", run.stderr)

    # ---- the apply pass ----------------------------------------------------------

    def test_apply_order_and_submits(self):
        run = self.run_delta(env_extra={"DELTA_APPLY": "1", "BACKUP_ID": "20261011_015806"})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("backup: 20261011_015806", run.stdout)
        calls = signatures(run.log)
        self.assertEqual(calls[0], ("delta_counts.py", ()))
        self.assertEqual(calls[1:15], DRY_P1)
        # the apply pass: the loaders in order, between the two dry passes
        p2 = calls[15:31]
        self.assertEqual(
            [c for c in p2 if c[1]][:6],
            [
                ("import_sales.py", ("--apply",)),
                ("import_credit_note.py", ("--write",)),
                ("import_purchase.py", ("--apply",)),
                ("import_payments_in.py", ("--write",)),
                ("import_payments_out.py", ("--write",)),
                ("import_vat_fix.py", ("--write",)),
            ],
        )
        self.assertIn(("import_master.py", ()), p2, "master's apply mode is the default, no flag")
        self.assertIn(("import_bank.py", ("--check",)), p2)
        self.assertIn(("import_payroll.py", ("--check",)), p2)
        self.assertIn(("import_files.py", ("--apply",)), p2)
        self.assertIn(("import_document_pdfs.py", ("--apply",)), p2)
        self.assertIn(("import_opening.py", ("--dry-run",)), p2)
        self.assertEqual(calls[31], ("delta_counts.py", ()))
        self.assertEqual(calls[32:46], DRY_P1, "the rerun is the dry pass again")
        self.assertEqual(calls[46], ("delta_counts.py", ()))
        self.assertEqual(calls[47], ("check_trial_balance.py", ()))
        with open(run.dlog) as f:
            docker_calls = f.read()
        self.assertEqual(docker_calls.count("MODE=submit"), 10, docker_calls)
        # the plans of the write loaders land in the run directory
        run_dirs = os.listdir(os.path.join(run.priv, "bexio-delta"))
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue(os.path.exists(os.path.join(run.priv, "bexio-delta", run_dirs[0], "p2", "bank.json")))
        self.assertFalse(os.path.exists(os.path.join(run.priv, ".bexio-delta.lock")))

    def canned_output(self, text):
        path = tempfile.mktemp()
        self.addCleanup(subprocess.run, ["rm", "-f", path])
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_apply_counts_table_sums_the_loader_lines(self):
        out = MASTER_TABLE + "Sales Invoice created 2\nSales Invoice skipped 9\nPayment Entry failed 1\n"
        run = self.run_delta(
            env_extra={"DELTA_APPLY": "1", "BACKUP_ID": "b1", "DELTA_OUT": self.canned_output(out)}
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = table_rows(run.stdout)
        self.assertEqual(rows["master"], ["0", "1", "206", "0", "0"])
        # the loader lines are in the importer's own output (sales and purchase run their own
        # loader before the submit); the submit logs are empty under the stubbed docker
        self.assertEqual(rows["sales"], ["2", "0", "9", "1", "0"])
        self.assertEqual(rows["purchase"], ["2", "0", "9", "1", "0"])
        # payments run no loader of their own: their row comes from the submit run only,
        # and the stubbed loader's empty output is a zero row
        self.assertEqual(rows["payments in"], ["0", "0", "0", "0", "0"])
        self.assertEqual(rows["trial balance"], ["-", "-", "-", "-", "0"])

    def test_sales_counts_stop_at_the_chf_table(self):
        """The dry output's second table (CHF by year) must not sum into the counts."""
        out = (
            "export             records  mapped  unmapped  differences  in ERPNext  to create\n"
            "invoices               111     111         0           37         111          0\n"
            "deliveries              13      13         0           13          13          2\n"
            "\n"
            "CHF by year of the document date (net, tax, gross):\n"
            "  invoices        2021      3 docs        1,000.00       81.00      1,081.00\n"
            "  orders          2025      5 docs        2,000.00      162.00      2,162.00\n"
            "dry run: nothing was written\n"
        )
        run = self.run_delta(env_extra={"DELTA_OUT": self.canned_output(out)})
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = table_rows(run.stdout)
        self.assertEqual(rows["sales"], ["2", "0", "124", "0", "0"])

    def test_rerun_must_insert_nothing(self):
        out = "\n".join(
            "Currency                5        0         8       0        0" if line.startswith("Currency") else line
            for line in MASTER_TABLE.splitlines()
        ) + "\n"
        run = self.run_delta(
            env_extra={"DELTA_APPLY": "1", "BACKUP_ID": "b1", "DELTA_OUT": self.canned_output(out)}
        )
        self.assertEqual(run.returncode, 4)
        self.assertIn("the rerun would insert 5 in master", run.stderr)

    def test_a_failed_step_stops_the_run(self):
        run = self.run_delta(env_extra={"DELTA_APPLY": "1", "BACKUP_ID": "b1", "DELTA_RC": "2"})
        self.assertEqual(run.returncode, 3)
        self.assertIn("aborted", run.stderr)
        calls = signatures(run.log)
        self.assertEqual(calls, [("delta_counts.py", ()), ("import_master.py", ("--dry-run",))])

    def test_rerun_must_not_move_the_counts(self):
        seq = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", seq])
        for i, count in enumerate(("Sales Invoice\t10", "Sales Invoice\t12", "Sales Invoice\t13"), start=1):
            with open(os.path.join(seq, "{}.txt".format(i)), "w") as f:
                f.write(count)
        run = self.run_delta(env_extra={"DELTA_APPLY": "1", "BACKUP_ID": "b1", "DELTA_COUNTS_SEQ_DIR": seq})
        self.assertEqual(run.returncode, 5)
        self.assertIn("rerun moved", run.stderr)
        self.assertIn("Sales Invoice", run.stderr)


if __name__ == "__main__":
    unittest.main()
