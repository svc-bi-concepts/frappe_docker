#!/bin/sh
# The hourly bexio -> ERPNext sync (erp-q1tm): keeps ERPNext current with NEW bexio
# documents during the working day, as DRAFTS only. A launchd job (ch.bi-concepts.bexio-sync,
# beside this script) runs it Mon-Fri 08:00-18:00 on the mini; by hand:
#
#   sh finance/bexio/sync_hourly.sh             the hourly run (writes drafts)
#   sh finance/bexio/sync_hourly.sh --dry-run   the same run, nothing written to ERPNext
#
# One run:
#
#   1. exports from bexio through the blessed read launcher finance/bexio/export.py,
#      only the entities the sales and purchase loaders read, into the day's
#      private/bexio-export/<date>/ (the av line injects the vault tokens; no write-capable
#      bexio credential rides along, and the export never writes to bexio),
#   2. runs import_sales.py and import_purchase.py in --apply mode (drafts only; the plans
#      are never submitted -- the submit stays the monthly delta's, with BACKUP_ID),
#   3. reads delta_counts.py before and after: the gate. Only bexio_id-keyed documents
#      move, and only by what the loaders report as created; a dry run moves nothing.
#   4. on failure or surprise, mails yardmaster AND Benchi (yardr mail), and
#   5. keeps every step's log in private/bexio-sync/<stamp>/ (mode 700), the last
#      BEXIO_SYNC_KEEP run folders (48, about two working days).
#
# SAFETY: no submits, no GL writes, no delta.sh apply pass; the loaders are idempotent by
# bexio_id, so a rerun inserts nothing. A lock under private/ refuses a second hourly run
# while one runs, and yields to a live monthly delta (which stays manual, outside the
# window). ERPNext access is api-agent@finance.local (.erpnext-api), unchanged.
#
# Exit codes: 0 ok; 2 refused (usage, lock held, a monthly delta holds its lock);
# 3 a step failed (mailed); 4 a surprise in the counts (mailed).
set -eu

usage() {
  cat <<'EOF'
usage: sh finance/bexio/sync_hourly.sh [--dry-run]

--dry-run  exports from bexio and reads ERPNext, writes nothing (the manual check
           against live); without it the loaders run in --apply mode, drafts only.

Exit codes: 0 ok; 2 refused; 3 a step failed (mailed); 4 a surprise in the counts (mailed).
EOF
}

REPO=$(cd "$(dirname "$0")/../.." && pwd)
BEXIO="$REPO/finance/bexio"
# The yard root: private/'s parent. yardr mail runs from there, so the mail lands
# in this yard's inboxes whatever the working directory launchd chose.
PRIVATE=${BEXIO_PRIVATE:-${PRIVATE:-/Users/bsaladin/ws_yardr_finance/private}}
export BEXIO_PRIVATE="$PRIVATE"
YARD=$(dirname "$PRIVATE")
BEXIO_DIR="$BEXIO"
export BEXIO_DIR
# launchd's PATH is minimal; docker (bexio-drafts.sh), yardr (the mail) and av (the
# export launcher) live in the homebrew and local prefixes. They go BEHIND the PATH the
# run already has, so a caller's own tools (the tests' stubs among them) stay first.
PATH="$PATH:$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin"
export PATH

SYNCDIR="$PRIVATE/bexio-sync"
LOCK="$PRIVATE/.bexio-sync.lock"
DELTA_LOCK="$PRIVATE/.bexio-delta.lock"
KEEP=${BEXIO_SYNC_KEEP:-48}
EXPORTER=${BEXIO_SYNC_EXPORTER:-"$BEXIO/export.py"}
# What the sales and purchase loaders read (import_sales.DOCUMENTS and APPLY_FILES,
# import_purchase's bills and expenses, the currencies and the journal both read,
# the accounts for the booked check). Nothing else: no files, no PDFs, no payroll.
ONLY_ENTITIES="invoices credit_vouchers orders offers deliveries currencies accounts journal bills expenses"

case "$KEEP" in
"" | *[!0-9]* | 0)
  echo "sync_hourly: BEXIO_SYNC_KEEP is a number of run folders to keep, not $KEEP" >&2
  exit 2
  ;;
esac

MODE="write"
for arg in "$@"; do
  case $arg in
  --help | -h)
    usage
    exit 0
    ;;
  --dry-run)
    MODE="dry"
    ;;
  *)
    echo "sync_hourly: unknown argument $arg" >&2
    usage >&2
    exit 2
    ;;
  esac
done
if [ ! -f "$EXPORTER" ]; then
  echo "sync_hourly: the export launcher $EXPORTER is missing" >&2
  exit 2
fi
if [ ! -f "$PRIVATE/.erpnext-api" ] && [ ! -f "$YARD/.erpnext-api" ]; then
  echo "sync_hourly: no .erpnext-api token at $YARD (ERPNext access)" >&2
  exit 2
fi

mkdir -p "$SYNCDIR"
chmod 700 "$SYNCDIR" 2>/dev/null || true

# ---- the run folders: keep the last KEEP -----------------------------------------

# ls for mtime order: the folder names are stamps this script made, nothing user-typed.
# shellcheck disable=SC2012
ls -1dt "$SYNCDIR"/2* 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  rm -rf "$old"
done

refuse() {
  echo "sync_hourly: $*" >&2
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >>"$SYNCDIR/refused.log"
  exit 2
}

# ---- the lock: one sync at a time, and never beside a live monthly delta ---------

if [ -d "$DELTA_LOCK" ]; then
  delta_pid=$(cut -d ' ' -f1 "$DELTA_LOCK/holder" 2>/dev/null || echo "?")
  if [ "$delta_pid" != "?" ] && kill -0 "$delta_pid" 2>/dev/null; then
    refuse "the monthly delta holds $DELTA_LOCK (pid $delta_pid); the sync waits for the next hour"
  fi
fi

cleanup() { rm -rf "$LOCK"; }
acquire_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    echo "$$ $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$LOCK/holder"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    return 0
  fi
  holder_pid=$(cut -d ' ' -f1 "$LOCK/holder" 2>/dev/null || echo "?")
  if [ "$holder_pid" != "?" ] && kill -0 "$holder_pid" 2>/dev/null; then
    refuse "another sync holds $LOCK (pid $holder_pid); it does not run twice"
  fi
  echo "sync_hourly: $LOCK is stale (pid ${holder_pid:-?} is gone), taking it over" >&2
  cleanup
  acquire_lock
}
acquire_lock

# ---- the run directory -----------------------------------------------------------

STAMP=$(date +%Y%m%d-%H%M%S)
RUNDIR="$SYNCDIR/$STAMP"
mkdir -p "$RUNDIR"
chmod 700 "$RUNDIR"
EXPORT_DIR="$PRIVATE/bexio-export/$(date +%F)"

RC=0
run_step() { # run_step <name> <outfile|-> <command...>: run, keep the log, print it, record the rc.
# outfile is where stdout goes (the counts snapshots the gate reads); "-" folds stdout into
# the log, the way delta.sh keeps a step's output.
  step=$1
  outfile=$2
  shift 2
  log="$RUNDIR/$step.log"
  echo "== $step"
  RC=0
  if [ "$outfile" = "-" ]; then
    "$@" >"$log" 2>&1 || RC=$?
  else
    "$@" >"$outfile" 2>"$log" || RC=$?
  fi
  cat "$log"
  echo "$RC" >"$RUNDIR/$step.rc"
  return "$RC"
}

# ---- the loaders' created counts: the loader's rows "doctype status count" -------

created_from() { # created_from <log>: "doctype<TAB>created" for the two invoice doctypes
  awk '
    NF == 4 && $4 ~ /^[0-9]+$/ && $3 == "created" && \
      ($1 " " $2 == "Sales Invoice" || $1 " " $2 == "Purchase Invoice") {
      n[$1 " " $2] += $4
    }
    END { printf "Sales Invoice\t%d\nPurchase Invoice\t%d\n", n["Sales Invoice"], n["Purchase Invoice"] }
  ' "$1"
}

# ---- the gate: what moved between the two snapshots ------------------------------

# The table in one awk pass (a shell loop would split "Sales Invoice" on the space):
# before and after are the delta_counts snapshots, the created files hold what the
# loaders reported. Write mode expects each counted doctype to move exactly by its
# created count; a dry run expects nothing to move. The exit status says surprise.
gate_table() { # gate_table <mode> <counts-before> <counts-after> <created-sales> <created-purchase>
  awk -F '\t' -v mode="$1" '
    BEGIN {
      n = split("Sales Invoice;Purchase Invoice;Payment Entry;Journal Entry;Bank Transaction;File", order, ";")
      printf "doctype             %8s %8s %8s %8s  gate\n", "before", "after", "move", "expect"
    }
    FILENAME ~ /counts-before$/ { before[$1] = $2 + 0; next }
    FILENAME ~ /counts-after$/ { after[$1] = $2 + 0; next }
    FILENAME ~ /created-sales$/ { sc[$1] = $2 + 0; next }
    FILENAME ~ /created-purchase$/ { pc[$1] = $2 + 0; next }
    END {
      surprise = 0
      for (i = 1; i <= n; i++) {
        dt = order[i]
        move = after[dt] - before[dt]
        expect = 0
        if (mode == "write") expect = sc[dt] + pc[dt]
        v = (move == expect) ? "ok" : "SURPRISE"
        if (v == "SURPRISE") surprise++
        printf "%-19s %8d %8d %8d %8d  %s\n", dt, before[dt], after[dt], move, expect, v
      }
      exit surprise > 0 ? 1 : 0
    }
  ' "$2" "$3" "$4" "$5"
}

# ---- the run ---------------------------------------------------------------------

echo "bexio hourly sync ($MODE) at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "run dir: $RUNDIR"

FAIL=""
SURPRISE=""

# shellcheck disable=SC2086 # the entity list is one word per entity, by design
run_step export - "$EXPORTER" --out "$EXPORT_DIR" --only $ONLY_ENTITIES || {
  FAIL="the bexio export failed (exit $RC), see export.log"
}

if [ -z "$FAIL" ]; then
  run_step counts-before "$RUNDIR/counts-before" python3 "$BEXIO/delta_counts.py" || FAIL="delta_counts.py failed before the loaders (exit $RC)"
fi

LOADER_FLAG=--apply
if [ "$MODE" = dry ]; then
  LOADER_FLAG=--dry-run
fi

if [ -z "$FAIL" ]; then
  run_step sales - python3 "$BEXIO/import_sales.py" --export "$EXPORT_DIR" "$LOADER_FLAG" || RC=$?
  if [ "$RC" -ge 2 ] || { [ "$RC" -eq 1 ] && grep -q "Traceback\|aborted:" "$RUNDIR/sales.log"; }; then
    FAIL="import_sales.py failed (exit $RC), see sales.log"
  fi
else
  echo "== sales skipped (the export or the counts failed)"
fi

if [ -z "$FAIL" ]; then
  run_step purchase - python3 "$BEXIO/import_purchase.py" --export "$EXPORT_DIR" "$LOADER_FLAG" || RC=$?
  if [ "$RC" -ge 2 ] || { [ "$RC" -eq 1 ] && grep -q "Traceback\|aborted:" "$RUNDIR/purchase.log"; }; then
    FAIL="import_purchase.py failed (exit $RC), see purchase.log"
  fi
else
  echo "== purchase skipped (the export or the counts failed)"
fi

if [ -z "$FAIL" ]; then
  run_step counts-after "$RUNDIR/counts-after" python3 "$BEXIO/delta_counts.py" || FAIL="delta_counts.py failed after the loaders (exit $RC)"
fi

# ---- the gate --------------------------------------------------------------------

if [ -z "$FAIL" ] && [ -f "$RUNDIR/counts-before" ] && [ -f "$RUNDIR/counts-after" ]; then
  created_from "$RUNDIR/sales.log" >"$RUNDIR/created-sales"
  created_from "$RUNDIR/purchase.log" >"$RUNDIR/created-purchase"

  # The table goes to the file first (the exit status carries the surprise without a
  # subshell swallowing it), then to the screen.
  if ! gate_table "$MODE" "$RUNDIR/counts-before" "$RUNDIR/counts-after" \
    "$RUNDIR/created-sales" "$RUNDIR/created-purchase" >"$RUNDIR/gate.txt"; then
    SURPRISE=$(awk '$NF == "SURPRISE" {
      dt = $1
      for (i = 2; i <= NF - 5; i++) dt = dt " " $i
      printf "%s moved %s, expected %s; ", dt, $(NF - 2), $(NF - 1)
    }' "$RUNDIR/gate.txt")
  fi
  cat "$RUNDIR/gate.txt"
fi

VERDICT=ok
if [ -n "$FAIL" ]; then
  VERDICT=failed
elif [ -n "$SURPRISE" ]; then
  VERDICT=surprise
fi

{
  echo "bexio hourly sync: $VERDICT ($MODE)"
  echo "run dir: $RUNDIR"
  echo "export: $EXPORT_DIR"
  for step in export counts-before sales purchase counts-after; do
    rc="-"
    if [ -f "$RUNDIR/$step.rc" ]; then
      rc=$(cat "$RUNDIR/$step.rc")
    fi
    echo "  $step: exit $rc"
  done
  [ -z "$FAIL" ] || echo "failure: $FAIL"
  [ -z "$SURPRISE" ] || echo "surprise: $SURPRISE"
} | tee "$RUNDIR/summary.txt"

# ---- the mail: on failure or surprise, yardmaster AND Benchi ----------------------

if [ "$VERDICT" != ok ]; then
  TEXT="bexio hourly sync $VERDICT ($(date '+%F %H:%M'), $MODE)
$RUNDIR
${FAIL:-}${SURPRISE:-}
The step logs and the counts table are in the run dir."
  MAIL_LOG="$RUNDIR/mail.log"
  : >"$MAIL_LOG"
  for who in yardmaster Benchi; do
    if (cd "$YARD" && yardr mail "$who" "$TEXT") >>"$MAIL_LOG" 2>&1; then
      echo "mailed $who" | tee -a "$MAIL_LOG"
    else
      echo "mail to $who FAILED" | tee -a "$MAIL_LOG"
    fi
  done
fi

case $VERDICT in
failed) exit 3 ;;
surprise) exit 4 ;;
*) exit 0 ;;
esac
