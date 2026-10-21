"""Offline tests for sync_hourly.sh: the modes, the gate and the mail.

No bexio, no ERPNext, no docker, no vault, no yardr: python3 is a stub on PATH that
prints canned loader rows and canned delta_counts snapshots, the export launcher is a
stub script (the real one's first line would run av against the vault), and yardr is a
stub that records the mail. private/ and the yard root are temporary (private_guard).
Run with: cd finance/bexio && python3 -m unittest test_sync_hourly
"""

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest

from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "bexio", "sync_hourly.sh")
PLIST = os.path.join(ROOT, "finance", "bexio", "ch.bi-concepts.bexio-sync.plist")

ONLY = "invoices credit_vouchers orders offers deliveries currencies accounts journal bills expenses"
# export.py takes ONE entity per --only flag (argparse append); the wrapper repeats the flag.
ONLY_ARGS = " ".join("--only " + entity for entity in ONLY.split())

# The loader's row, as bexio-drafts.py prints it: doctype, status, count.
CREATED_SALES_2 = "Sales Invoice          created         2\n"
CREATED_PURCHASE_1 = "Purchase Invoice       created         1\n"
TRACEBACK = "Traceback (most recent call last):\nsubprocess.CalledProcessError: command exited 1\n"

COUNTS = "Sales Invoice\t{}\nPurchase Invoice\t{}\nPayment Entry\t{}\nJournal Entry\t3\nBank Transaction\t9\nFile\t100\n"
COUNTS_BEFORE = COUNTS.format(10, 5, 7)
COUNTS_AFTER_CLEAN = COUNTS.format(
    12, 6, 7
)  # sales created 2, purchase created 1, the rest still
COUNTS_AFTER_SI_15 = COUNTS.format(
    15, 6, 7
)  # Sales Invoice moved 5, sales created 2, purchase its 1
COUNTS_AFTER_PE_8 = COUNTS.format(10, 5, 8)  # a doctype no loader writes moved

PYTHON_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$SYNC_LOG"
case "$*" in
*delta_counts.py*)
  if [ -n "${COUNTS_SEQ_DIR:-}" ]; then
    n=$(ls "$COUNTS_SEQ_DIR" 2>/dev/null | grep -c '^call-' || true)
    n=$((n + 1))
    : >"$COUNTS_SEQ_DIR/call-$n"
    [ -f "$COUNTS_SEQ_DIR/$n.txt" ] && cat "$COUNTS_SEQ_DIR/$n.txt"
  fi
  rc=${COUNTS_RC:-0}
  if [ "$n" = "2" ] && [ -n "${COUNTS_RC2:-}" ]; then rc=$COUNTS_RC2; fi
  exit $rc
  ;;
*import_sales.py*)
  [ -n "${SALES_OUT:-}" ] && cat "$SALES_OUT"
  exit ${SALES_RC:-0}
  ;;
*import_purchase.py*)
  [ -n "${PURCHASE_OUT:-}" ] && cat "$PURCHASE_OUT"
  exit ${PURCHASE_RC:-0}
  ;;
esac
exit 0
"""

EXPORTER_STUB = """#!/bin/sh
printf 'exporter %s\\n' "$*" >>"$SYNC_LOG"
exit ${EXPORTER_RC:-0}
"""

# colima-wake stands in for IT's shim: it logs its args to WAKE_LOG (not SYNC_LOG, so the
# run's call order stays the run's own), notes if a step had already run (SYNC_LOG is
# written by the first step), and exits WAKE_RC.
WAKE_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$WAKE_LOG"
[ -s "$SYNC_LOG" ] && printf 'after-steps\\n' >>"$WAKE_LOG"
exit ${WAKE_RC:-0}
"""

YARDR_STUB = """#!/bin/sh
# the text lands on one line, so the test can read recipient<TAB>text per line
printf '%s\\t%s\\n' "$2" "$(printf '%s' "$3" | tr '\\n' ' ')" >>"$YARDR_MAIL_LOG"
# the env the wrapper handed the binary: the yard's YARDR_HOME must ride along (erp-xgpk)
printf '%s\\n' "${YARDR_HOME-UNSET}" >>"$YARDR_ENV_LOG"
# and the actor: launchd's $USER is no yard name, so the wrapper must name one (erp-xwht)
printf '%s\\n' "${YARDR_ACTOR-UNSET}" >>"$YARDR_ACTOR_LOG"
exit ${YARDR_RC:-0}
"""


class SyncHourlyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="sync-hourly-test-")
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        # HOME points at the temp folder, so the wrapper's PATH prepend cannot find a
        # real yardr (the mail) or a real av ahead of the stubs.
        self.home = os.path.join(self.temp, "home")
        self.stubdir = os.path.join(self.temp, "stubs")
        os.makedirs(self.home)
        os.makedirs(self.stubdir)
        for name, body in (("python3", PYTHON_STUB), ("yardr", YARDR_STUB)):
            path = os.path.join(self.stubdir, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.exporter = os.path.join(self.stubdir, "exporter-stub")
        with open(self.exporter, "w") as f:
            f.write(EXPORTER_STUB)
        os.chmod(self.exporter, 0o755)
        self.wake = os.path.join(self.stubdir, "colima-wake-stub")
        with open(self.wake, "w") as f:
            f.write(WAKE_STUB)
        os.chmod(self.wake, 0o755)
        self.wakelog = os.path.join(self.temp, "wake.log")
        self.log = os.path.join(self.temp, "calls.log")
        self.maillog = os.path.join(self.temp, "mail.log")
        self.envlog = os.path.join(self.temp, "yardr-env.log")
        self.actorlog = os.path.join(self.temp, "yardr-actor.log")
        self.seqdir = os.path.join(self.temp, "counts-seq")
        os.makedirs(self.seqdir)
        # Each test gets its own private folder (the guard's shared one would carry run
        # folders and locks from the test before). The wrapper reads BEXIO_PRIVATE.
        self.private = os.path.join(self.temp, "private")
        os.makedirs(self.private)
        with open(os.path.join(self.private, ".erpnext-api"), "w") as f:
            f.write("token-for-tests")
        self.syncdir = os.path.join(self.private, "bexio-sync")

    def env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = self.stubdir + os.pathsep + env.get("PATH", "")
        env["HOME"] = self.home
        env["SYNC_LOG"] = self.log
        env["COUNTS_SEQ_DIR"] = self.seqdir
        env["BEXIO_PRIVATE"] = self.private
        env["BEXIO_SYNC_EXPORTER"] = self.exporter
        # Always a stub: the real colima-wake would start the VM on this machine.
        env["COLIMA_WAKE"] = self.wake
        env["WAKE_LOG"] = self.wakelog
        env["BEXIO_SYNC_YARDR"] = os.path.join(self.stubdir, "yardr")
        env["YARDR_MAIL_LOG"] = self.maillog
        env["YARDR_ENV_LOG"] = self.envlog
        env["YARDR_ACTOR_LOG"] = self.actorlog
        env.pop("BEXIO_SYNC_KEEP", None)
        # launchd's bare env has no YARDR_HOME or YARDR_ACTOR; the tests run like that unless
        # one sets it. The outer session's YARDR_ACTOR (a builder's own name) must not leak in.
        env.pop("YARDR_HOME", None)
        env.pop("YARDR_ACTOR", None)
        env.pop("BEXIO_SYNC_ACTOR", None)
        env.update(extra)
        return env

    def run_script(self, *args, **extra):
        return subprocess.run(
            ["sh", SCRIPT, *args],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def counts_seq(self, before, after):
        with open(os.path.join(self.seqdir, "1.txt"), "w") as f:
            f.write(before)
        with open(os.path.join(self.seqdir, "2.txt"), "w") as f:
            f.write(after)

    def canned(self, name, body):
        path = os.path.join(self.temp, name)
        with open(path, "w") as f:
            f.write(body)
        return path

    def run_dir(self):
        stamps = [d for d in os.listdir(self.syncdir) if d[0].isdigit()]
        self.assertEqual(len(stamps), 1, stamps)
        return os.path.join(self.syncdir, stamps[0])

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def mails(self):
        if not os.path.exists(self.maillog):
            return []
        with open(self.maillog) as f:
            return [line.split("\t", 1) for line in f.read().splitlines()]

    def yardr_envs(self):
        """The YARDR_HOME each mail call saw, in order (the stub logs one line per call)."""
        if not os.path.exists(self.envlog):
            return []
        with open(self.envlog) as f:
            return f.read().splitlines()

    # ---- the clean runs ------------------------------------------------------------

    def test_dry_run_clean(self):
        """--dry-run: export, both loaders in --dry-run, nothing moved, no mail."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertEqual(len([c for c in calls if c.startswith("exporter ")]), 1)
        self.assertIn(ONLY_ARGS, calls[0])
        self.assertIn("--dry-run", " ".join(c for c in calls if "import_sales.py" in c))
        self.assertIn(
            "--dry-run", " ".join(c for c in calls if "import_purchase.py" in c)
        )
        self.assertEqual(self.mails(), [])
        with open(os.path.join(self.run_dir(), "summary.txt")) as f:
            self.assertIn("ok (dry)", f.read())

    def test_write_mode_clean(self):
        """The moves equal what the loaders created: exit 0, no mail, loaders in --apply."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_CLEAN)
        sales_out = self.canned("sales.out", CREATED_SALES_2)
        purchase_out = self.canned("purchase.out", CREATED_PURCHASE_1)
        proc = self.run_script(
            SALES_OUT=sales_out,
            PURCHASE_OUT=purchase_out,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertIn("--apply", " ".join(c for c in calls if "import_sales.py" in c))
        self.assertIn(
            "--apply", " ".join(c for c in calls if "import_purchase.py" in c)
        )
        self.assertEqual(self.mails(), [])
        rundir = self.run_dir()
        with open(os.path.join(rundir, "gate.txt")) as f:
            gate = f.read()
        self.assertIn("Sales Invoice", gate)
        self.assertNotIn("SURPRISE", gate)
        for step in ("export", "counts-before", "sales", "purchase", "counts-after"):
            with open(os.path.join(rundir, step + ".rc")) as f:
                self.assertEqual(f.read().strip(), "0", step)

    def test_export_targets_the_day_folder(self):
        """The export goes to private/bexio-export/<today>, the loaders read exactly it."""
        import datetime

        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        today = datetime.date.today().isoformat()
        export_dir = os.path.join(self.private, "bexio-export", today)
        self.assertIn("--out " + export_dir, self.calls()[0])
        sales_call = next(c for c in self.calls() if "import_sales.py" in c)
        self.assertIn("--export " + export_dir, sales_call)

    # ---- the surprises -------------------------------------------------------------

    def test_write_mode_invoice_mismatch_mails(self):
        """Sales Invoice moved 5 while the loader created 2: exit 4, yardmaster and Benchi."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_SI_15)
        sales_out = self.canned("sales.out", CREATED_SALES_2)
        purchase_out = self.canned("purchase.out", CREATED_PURCHASE_1)
        proc = self.run_script(SALES_OUT=sales_out, PURCHASE_OUT=purchase_out)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        mails = self.mails()
        self.assertEqual([m[0] for m in mails], ["yardmaster", "Benchi"])
        self.assertIn("Sales Invoice moved 5, expected 2", mails[0][1])
        self.assertIn(self.run_dir(), mails[0][1])

    def test_doctype_no_loader_writes_moved_mails(self):
        """A Payment Entry moved although no loader writes one: exit 4."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_PE_8)
        proc = self.run_script(
            SALES_OUT=self.canned("sales.out", CREATED_SALES_2),
            PURCHASE_OUT=self.canned("purchase.out", CREATED_PURCHASE_1),
        )
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("Payment Entry moved 1, expected 0", self.mails()[0][1])
        self.assertIn("Payment Entry moved 1, expected 0", self.mails()[1][1])

    def test_dry_run_moved_counts_mails(self):
        """A dry run must move nothing; a move is a surprise even with no loader rows."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_CLEAN)
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("Sales Invoice moved 2, expected 0", self.mails()[0][1])

    # ---- the failures --------------------------------------------------------------

    def test_export_failure_mails_and_skips_the_loaders(self):
        """The export failed: exit 3, one mail, no loader ran, no counts-after."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertNotIn("import_sales.py", " ".join(self.calls()))
        self.assertFalse(
            os.path.exists(os.path.join(self.run_dir(), "counts-after.rc"))
        )
        mails = self.mails()
        self.assertEqual([m[0] for m in mails], ["yardmaster", "Benchi"])
        self.assertIn("the bexio export failed (exit 3)", mails[0][1])

    def test_loader_abort_mails(self):
        """The loader's abort (exit 2, 'aborted:' on stderr, ERPNext unreachable) is a failure."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        sales_out = self.canned(
            "sales.out", "aborted: ERPNext at http://... did not answer\n"
        )
        proc = self.run_script(SALES_OUT=sales_out, SALES_RC="2")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("import_sales.py failed (exit 2)", self.mails()[0][1])

    def test_loader_crash_mails(self):
        """Exit 1 with a traceback is the loader's crash (check=True on the loader), not a difference."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        sales_out = self.canned("sales.out", TRACEBACK)
        proc = self.run_script(SALES_OUT=sales_out, SALES_RC="1")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("import_sales.py failed (exit 1)", self.mails()[0][1])

    def test_loader_differences_are_accepted(self):
        """Exit 1 without a traceback is the loaders' accepted differences: no failure, no mail."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_CLEAN)
        sales_out = self.canned(
            "sales.out", CREATED_SALES_2 + "sales differences: 3 lines\n"
        )
        purchase_out = self.canned(
            "purchase.out", CREATED_PURCHASE_1 + "1 record(s) listed by bexio id\n"
        )
        proc = self.run_script(
            SALES_OUT=sales_out,
            SALES_RC="1",
            PURCHASE_OUT=purchase_out,
            PURCHASE_RC="1",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.mails(), [])

    def test_counts_failure_mails(self):
        """delta_counts.py failing after the loaders is a failure, with the loaders' writes done."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_AFTER_CLEAN)
        os.remove(
            os.path.join(self.seqdir, "2.txt")
        )  # the after snapshot answers nothing
        proc = self.run_script(
            SALES_OUT=self.canned("sales.out", CREATED_SALES_2),
            PURCHASE_OUT=self.canned("purchase.out", CREATED_PURCHASE_1),
            COUNTS_RC2="3",
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("delta_counts.py failed after the loaders", self.mails()[0][1])
        with open(os.path.join(self.run_dir(), "counts-after.rc")) as f:
            self.assertEqual(f.read().strip(), "3")

    def yardr_actors(self):
        """The YARDR_ACTOR each mail call saw, in order (the stub logs one line per call)."""
        if not os.path.exists(self.actorlog):
            return []
        with open(self.actorlog) as f:
            return f.read().splitlines()

    # ---- the mail's actor (erp-xwht) -------------------------------------------------

    def test_mail_speaks_as_benchi_in_a_bare_env(self):
        """Launchd's bare env (no YARDR_ACTOR, $USER bsaladin): a failing run's two mails
        both go out as Benchi, the yard user whose job runs the sync."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual([m[0] for m in self.mails()], ["yardmaster", "Benchi"])
        self.assertEqual(self.yardr_actors(), ["Benchi"] * 2)

    def test_mail_actor_honors_the_override(self):
        """BEXIO_SYNC_ACTOR (for tests) wins over the Benchi default."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3", BEXIO_SYNC_ACTOR="Someone")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.yardr_actors(), ["Someone"] * 2)

    # ---- the mail's yard (erp-xgpk) --------------------------------------------------

    def test_mail_carries_the_yard_home_in_a_bare_env(self):
        """Launchd's bare env (no YARDR_HOME): each mail call still carries the yard's
        YARDR_HOME - a plain `yardr` from PATH would resolve the DEFAULT home and the
        failure mail would be lost."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual([m[0] for m in self.mails()], ["yardmaster", "Benchi"])
        self.assertEqual(
            self.yardr_envs(), ["/Users/bsaladin/ws_yardr_finance/.yardr"] * 2
        )

    def test_mail_honors_a_yardr_home_the_caller_set(self):
        """A YARDR_HOME in the environment wins over the wrapper's default (overridable)."""
        home = os.path.join(self.temp, "other-yard")
        os.makedirs(home)
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3", YARDR_HOME=home)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.yardr_envs(), [home] * 2)

    def test_mail_defaults_are_pinned_in_the_wrapper(self):
        """The defaults live in the wrapper (not only the plist): the yard's binary and
        the yard's home, so a manual run behaves like the launchd run."""
        with open(SCRIPT) as f:
            body = f.read()
        self.assertIn(
            "YARDR_BIN=${BEXIO_SYNC_YARDR:-/Users/bsaladin/ws_yardr/yardr}", body
        )
        self.assertIn(
            "YARDR_HOME=${YARDR_HOME:-/Users/bsaladin/ws_yardr_finance/.yardr}", body
        )

    def test_mail_failure_is_logged_per_recipient(self):
        """A yardr that fails must not mask the verdict: each recipient's failure lands
        in mail.log and the run still exits its own code (review, erp-xgpk)."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(EXPORTER_RC="3", YARDR_RC="1")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        with open(os.path.join(self.run_dir(), "mail.log")) as f:
            self.assertEqual(
                f.read().splitlines(),
                ["mail to yardmaster FAILED", "mail to Benchi FAILED"],
            )
        self.assertEqual(
            self.yardr_envs(), ["/Users/bsaladin/ws_yardr_finance/.yardr"] * 2
        )

    # ---- the colima VM (the wake before the run) -------------------------------------

    def wake_calls(self):
        if not os.path.exists(self.wakelog):
            return []
        with open(self.wakelog) as f:
            return f.read().splitlines()

    def test_wake_runs_once_before_the_first_step(self):
        """colima-wake --hold 15 --reason bexio-sync runs once, before the exporter (and so
        before any docker call the loaders make)."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # one call, with the args, and no step had run when it was made (no "after-steps")
        self.assertEqual(self.wake_calls(), ["--hold 15 --reason bexio-sync"])
        self.assertTrue(self.calls()[0].startswith("exporter "))

    def test_wake_failure_exits_before_any_step(self):
        """A failing wake: exit 3, one line on stderr, no docker call, no run folder."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(WAKE_RC="1")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("colima-wake failed", proc.stderr)
        self.assertFalse(os.path.exists(self.log))  # no step ran, so no step logged
        self.assertEqual(self.wake_calls(), ["--hold 15 --reason bexio-sync"])
        self.assertEqual([d for d in os.listdir(self.syncdir) if d[0].isdigit()], [])
        self.assertEqual(self.mails(), [])

    def test_missing_wake_is_skipped(self):
        """No shim on this machine (tests, other machines): the run goes on, silently."""
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(COLIMA_WAKE=os.path.join(self.temp, "no-such-wake"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("colima-wake", proc.stderr)
        self.assertEqual(self.wake_calls(), [])
        self.assertTrue(self.calls()[0].startswith("exporter "))

    def test_wake_is_skipped_for_a_refused_run(self):
        """A refused run (a live lock holder) does not wake the VM for nothing."""
        lock = os.path.join(self.private, ".bexio-sync.lock")
        os.makedirs(lock)
        self.addCleanup(shutil.rmtree, lock, True)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write(f"{os.getpid()} now\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self.wake_calls(), [])

    # ---- the locks -----------------------------------------------------------------

    def test_lock_refuses_a_second_run(self):
        """A live holder pid refuses the run: exit 2, refused.log, no run folder, no mail."""
        lock = os.path.join(self.private, ".bexio-sync.lock")
        os.makedirs(lock)
        self.addCleanup(shutil.rmtree, lock, True)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write(f"{os.getpid()} now\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        stamps = [d for d in os.listdir(self.syncdir) if d[0].isdigit()]
        self.assertEqual(stamps, [])
        with open(os.path.join(self.syncdir, "refused.log")) as f:
            self.assertIn("another sync holds", f.read())
        self.assertEqual(self.mails(), [])

    def test_stale_lock_is_taken_over(self):
        """A holder pid that is gone is a stale lock: the run takes it and removes it."""
        lock = os.path.join(self.private, ".bexio-sync.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write("999999 gone\n")
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(lock))

    def test_live_delta_lock_refuses_the_sync(self):
        """The sync yields to a live monthly delta: exit 2, no run folder."""
        lock = os.path.join(self.private, ".bexio-delta.lock")
        os.makedirs(lock)
        self.addCleanup(shutil.rmtree, lock, True)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write(f"{os.getpid()} now\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual([d for d in os.listdir(self.syncdir) if d[0].isdigit()], [])

    def test_stale_delta_lock_does_not_block(self):
        """A stale delta lock does not hold the sync up."""
        lock = os.path.join(self.private, ".bexio-delta.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "holder"), "w") as f:
            f.write("999999 gone\n")
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # ---- the run folders -----------------------------------------------------------

    def test_old_run_folders_are_pruned(self):
        """BEXIO_SYNC_KEEP run folders stay, the older ones go."""
        for i, stamp in enumerate(
            ["20260101-080000", "20260101-090000", "20260101-100000"]
        ):
            path = os.path.join(self.syncdir, stamp)
            os.makedirs(path)
            os.utime(path, (1000000 + i * 10000, 1000000 + i * 10000))
        self.counts_seq(COUNTS_BEFORE, COUNTS_BEFORE)
        proc = self.run_script(BEXIO_SYNC_KEEP="2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stamps = sorted(d for d in os.listdir(self.syncdir) if d[0].isdigit())
        self.assertEqual(len(stamps), 3)
        self.assertNotIn("20260101-080000", stamps)

    def test_keep_must_be_a_number(self):
        proc = self.run_script(BEXIO_SYNC_KEEP="lots")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BEXIO_SYNC_KEEP", proc.stderr)

    def test_refusal_without_the_erpnext_token(self):
        os.remove(os.path.join(self.private, ".erpnext-api"))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        self.assertIn(".erpnext-api", proc.stderr)

    def test_usage_rejects_unknown_arguments(self):
        proc = self.run_script("--write")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown argument", proc.stderr)

    def test_default_exporter_is_the_blessed_export(self):
        """The export runs through finance/bexio/export.py, whose av line injects only the read
        token (erp-19xn made it read-token-only and reads every entity, journal included);
        the interim export_readonly.py launcher is gone."""
        with open(SCRIPT) as f:
            body = f.read()
        self.assertIn('EXPORTER=${BEXIO_SYNC_EXPORTER:-"$BEXIO/export.py"}', body)
        self.assertNotIn("export_readonly", body)
        launcher = os.path.join(ROOT, "finance", "bexio", "export.py")
        self.assertTrue(os.path.exists(launcher))
        with open(launcher) as f:
            first = f.readline().strip()
        self.assertEqual(
            first, "#!/usr/local/bin/av inject +BEXIO_READ_TOKEN /bin/bash"
        )
        self.assertFalse(
            os.path.exists(os.path.join(ROOT, "finance", "bexio", "export_readonly.py"))
        )


class PlistTest(unittest.TestCase):
    """The LaunchAgent: the window Benchi decided, the landed checkout, the yard root."""

    def setUp(self):
        with open(PLIST, "rb") as f:
            self.plist = plistlib.load(f)

    def test_window_is_weekdays_eight_to_eighteen(self):
        entries = {
            (e["Weekday"], e["Hour"]) for e in self.plist["StartCalendarInterval"]
        }
        self.assertEqual(
            entries, {(d, h) for d in (1, 2, 3, 4, 5) for h in range(8, 19)}
        )
        for entry in self.plist["StartCalendarInterval"]:
            self.assertEqual(entry["Minute"], 0)

    def test_runs_the_landed_wrapper_from_the_yard_root(self):
        args = self.plist["ProgramArguments"]
        self.assertEqual(args[0], "/bin/sh")
        self.assertEqual(
            args[1],
            "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/bexio/sync_hourly.sh",
        )
        self.assertEqual(
            self.plist["WorkingDirectory"], "/Users/bsaladin/ws_yardr_finance"
        )

    def test_calendar_only_and_logs_under_bexio_sync(self):
        self.assertNotIn("KeepAlive", self.plist)
        self.assertNotIn("RunAtLoad", self.plist)
        self.assertNotIn("StartInterval", self.plist)
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertIn(
                os.path.join("private", "bexio-sync", "launchd.log"), self.plist[key]
            )

    def test_label(self):
        self.assertEqual(self.plist["Label"], "ch.bi-concepts.bexio-sync")


if __name__ == "__main__":
    unittest.main()
