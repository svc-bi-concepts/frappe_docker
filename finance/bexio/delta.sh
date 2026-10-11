#!/bin/sh
# The monthly bexio delta (cutover 2027, erp-x3br): the import chain of finance/bexio/README.md
# as one command, run after every bexio month close until the cutover. It runs
#
#   master, sales (+ the credit note), purchase, payments in, payments out, VAT fix, bank,
#   manual entries, manual fix, payroll journal lines, opening, files, document PDFs
#
# first as a dry pass that prints each step's counts, then, with DELTA_APPLY=1, as an apply
# pass in which every plan is submitted through finance/scripts/bexio-drafts.sh (the GL is
# written), then as a dry pass again: the rerun, which must insert nothing. It ends with the
# trial balance check against bexio's journal (read only).
#
#   sh finance/bexio/delta.sh <export-dir>
#       dry pass and trial balance check; nothing is written to ERPNext.
#
#   DELTA_APPLY=1 BACKUP_ID=<id> sh finance/bexio/delta.sh <export-dir>
#       dry pass, apply pass, rerun, trial balance check. BACKUP_ID is the id of the backup
#       taken before (bench --site frontend backup); the run is refused without one. A lock
#       under private/ refuses a second delta while one runs.
#
# Each step's output is printed and kept in <private>/bexio-delta/<stamp>/<pass>/<step>.log,
# the plans beside them. The counts table at the end is what goes into the note. Exit codes:
# 0 ok; 2 refused (usage, no BACKUP_ID, lock held); 3 a step failed; 4 the rerun inserted
# something; 5 the rerun moved the bexio-keyed document counts.
#
# A clean rerun still has loaders that exit 1 for their accepted differences (the sales
# differences lines, the one unmapped credit-voucher payment, the bank's unmatched lines):
# the gate is that the rerun inserts nothing, not the loaders' exit codes.
set -eu

usage() {
  cat <<'EOF'
usage: sh finance/bexio/delta.sh <export-dir>
       DELTA_APPLY=1 BACKUP_ID=<id> sh finance/bexio/delta.sh <export-dir>

<export-dir>  a bexio export directory (under private/bexio-export/), passed to every loader
DELTA_APPLY   1 runs the apply pass (each plan submitted, the GL written) between the two
              dry passes; without it, dry pass and trial balance check only
BACKUP_ID     the id of the backup taken before the apply pass (bench --site frontend backup)

Exit codes: 0 ok; 2 refused; 3 a step failed; 4 the rerun inserted something;
5 the rerun moved the bexio-keyed document counts.
EOF
}

REPO=$(cd "$(dirname "$0")/../.." && pwd)
BEXIO="$REPO/finance/bexio"
PRIVATE=${PRIVATE:-/Users/bsaladin/ws_yardr_finance/private}
LOADER="$REPO/finance/scripts/bexio-drafts.sh"

EXPORT=""
for arg in "$@"; do
  case $arg in
  --help | -h)
    usage
    exit 0
    ;;
  -*)
    echo "delta.sh: unknown option $arg" >&2
    usage >&2
    exit 2
    ;;
  *)
    if [ -n "$EXPORT" ]; then
      echo "delta.sh: one export directory, not both $EXPORT and $arg" >&2
      exit 2
    fi
    EXPORT=$arg
    ;;
  esac
done
if [ -z "$EXPORT" ]; then
  usage >&2
  exit 2
fi
if [ ! -d "$EXPORT" ]; then
  echo "delta.sh: $EXPORT is not a directory" >&2
  exit 2
fi

case "${DELTA_APPLY:-0}" in
0) ;;
1) ;;
*)
  echo "delta.sh: DELTA_APPLY is 0 or 1, not $DELTA_APPLY" >&2
  exit 2
  ;;
esac
if [ "${DELTA_APPLY:-0}" = 1 ] && [ -z "${BACKUP_ID:-}" ]; then
  echo "delta.sh: DELTA_APPLY=1 needs BACKUP_ID, the id of the backup taken before" >&2
  exit 2
fi

# ---- the lock: one delta at a time -----------------------------------------------

LOCK="$PRIVATE/.bexio-delta.lock"
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
    echo "delta.sh: another delta run holds $LOCK (pid $holder_pid); it does not run twice" >&2
    exit 2
  fi
  echo "delta.sh: $LOCK is stale (pid ${holder_pid:-?} is gone), taking it over" >&2
  cleanup
  acquire_lock
}
acquire_lock

# ---- the run directory -----------------------------------------------------------

STAMP=$(date +%Y%m%d-%H%M%S)
RUNDIR="$PRIVATE/bexio-delta/$STAMP"
mkdir -p "$RUNDIR/p1" "$RUNDIR/p2" "$RUNDIR/p3" "$RUNDIR/run"
chmod 700 "$RUNDIR"

# ---- one step: run, print, keep the log, the exit code and the counts ------------

RC=0
run_step() { # run_step <pass> <step> <extractor> <command...>
  pass=$1
  step=$2
  extractor=$3
  shift 3
  log="$RUNDIR/$pass/$step.log"
  echo "== $pass/$step"
  RC=0
  "$@" >"$log" 2>&1 || RC=$?
  cat "$log"
  echo "$RC" >"$RUNDIR/$pass/$step.rc"
  if [ "$extractor" != "-" ]; then
    "$extractor" "$log" >"$RUNDIR/$pass/$step.counts"
  fi
  return "$RC"
}

die() {
  echo "delta.sh: $*" >&2
  exit 3
}

importer() { # importer <pass> <step> <extractor> <command...> — fatal when the loader aborts
  pass=$1
  step=$2
  run_step "$@" || RC=$?
  if [ "$RC" -eq 2 ]; then
    die "$step aborted (exit 2), see $RUNDIR/$pass/$step.log"
  fi
}

submit() { # submit <pass> <step> <plan.json> — every plan is submitted, so the GL is written
  pass=$1
  step=$2
  run_step "$1" "$2" extract_loader sh "$LOADER" "$3" submit || RC=$?
  if [ "$RC" -ne 0 ]; then
    die "$step failed (exit $RC), see $RUNDIR/$pass/$step.log"
  fi
}

check_step() { # check_step <pass> <step> <command...> — read only, recorded, never fatal
  run_step "$@" || true
}

# ---- the count extractors: "new changed skipped failed" per step -----------------

extract_master() { # the doctype table: created updated unchanged failed skipped
  awk '
    $1 == "doctype" && $2 == "created" { see = 1; next }
    see && NF == 6 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ && $6 ~ /^[0-9]+$/ {
      n += $2; c += $3; s += $4 + $6; f += $5
    }
    END { printf "%d %d %d %d\n", n, c, s, f }
  ' "$1"
}

extract_sales() { # the dry table: records mapped unmapped differences "in ERPNext" "to create"
  awk '
    $1 == "export" && $9 == "create" { see = 1; next }
    see && $1 == "CHF" { see = 0 }
    see && NF == 7 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ && $6 ~ /^[0-9]+$/ && $7 ~ /^[0-9]+$/ {
      n += $7; s += $6
    }
    END { printf "%d %d %d %d\n", n, 0, s, 0 }
  ' "$1"
}

extract_files() { # doctype files bytes attach bytes attached problems
  awk '
    $1 == "doctype" && $2 == "files" { see = 1; next }
    see && NF == 7 && $2 ~ /^[0-9]+$/ { n += $4; s += $6; f += $7 }
    END { printf "%d %d %d %d\n", n, 0, s, f }
  ' "$1"
}

extract_pdfs() { # kind pdfs bytes attach attached problems
  awk '
    $1 == "kind" && $2 == "pdfs" { see = 1; next }
    see && NF == 6 && $2 ~ /^[0-9]+$/ { n += $4; s += $5; f += $6 }
    END { printf "%d %d %d %d\n", n, 0, s, f }
  ' "$1"
}

extract_fix() { # "live groups N, corrections N, unchanged N, journal wins N, not live N"
  awk '
    $1 == "live" {
      for (i = 1; i < NF; i++) {
        if ($i == "corrections") n += $(i + 1)
        if ($i == "unchanged") s += $(i + 1)
      }
    }
    END { printf "%d %d %d %d\n", n, 0, s, 0 }
  ' "$1"
}

extract_loader() { # the loader's rows: doctype (two words), status, count; its scalar rows stay out
  awk '
    NF == 4 && $4 ~ /^[0-9]+$/ && \
      ($1 " " $2 == "Sales Invoice" || $1 " " $2 == "Purchase Invoice" || $1 " " $2 == "Payment Entry" || \
       $1 " " $2 == "Journal Entry" || $1 " " $2 == "Bank Transaction") {
      if ($3 == "created") n += $4
      else if ($3 == "updated") c += $4
      else if ($3 == "unchanged" || $3 == "skipped" || $3 == "conflict") s += $4
      else if ($3 == "failed") f += $4
    }
    END { printf "%d %d %d %d\n", n, c, s, f }
  ' "$1"
}

# ---- the chain -------------------------------------------------------------------

chain_dry() { # chain_dry <pass>
  p=$1
  importer "$p" master extract_master python3 "$BEXIO/import_master.py" --export "$EXPORT" --dry-run
  importer "$p" sales extract_sales python3 "$BEXIO/import_sales.py" --export "$EXPORT" --dry-run
  importer "$p" credit-note - python3 "$BEXIO/import_credit_note.py" --export "$EXPORT" --write "$RUNDIR/$p/credit-note.json"
  importer "$p" purchase - python3 "$BEXIO/import_purchase.py" --export "$EXPORT" --dry-run
  importer "$p" payments-in - python3 "$BEXIO/import_payments_in.py" --export "$EXPORT" --dry-run
  importer "$p" payments-out - python3 "$BEXIO/import_payments_out.py" --export "$EXPORT" --dry-run
  importer "$p" vat-fix - python3 "$BEXIO/import_vat_fix.py" --export "$EXPORT" --dry-run
  importer "$p" bank - python3 "$BEXIO/import_bank.py" --export "$EXPORT" --dry-run
  importer "$p" manual-entries - python3 "$BEXIO/import_manual_entries.py" --export "$EXPORT" --dry-run
  importer "$p" manual-fix extract_fix python3 "$BEXIO/import_manual_fix.py" --export "$EXPORT" --dry-run
  importer "$p" payroll - python3 "$BEXIO/import_payroll.py" --export "$EXPORT" --dry-run
  importer "$p" opening - python3 "$BEXIO/import_opening.py" --export "$EXPORT" --dry-run
  importer "$p" files extract_files python3 "$BEXIO/import_files.py" --export "$EXPORT" --dry-run
  importer "$p" document-pdfs extract_pdfs python3 "$BEXIO/import_document_pdfs.py" --export "$EXPORT" --dry-run
}

chain_apply() {
  p=p2
  importer "$p" master extract_master python3 "$BEXIO/import_master.py" --export "$EXPORT"
  importer "$p" sales extract_loader python3 "$BEXIO/import_sales.py" --export "$EXPORT" --apply
  submit "$p" sales-submit "$PRIVATE/bexio-sales-drafts.json"
  importer "$p" credit-note extract_loader python3 "$BEXIO/import_credit_note.py" --export "$EXPORT" --write "$RUNDIR/$p/credit-note.json"
  submit "$p" credit-note-submit "$RUNDIR/$p/credit-note.json"
  importer "$p" purchase extract_loader python3 "$BEXIO/import_purchase.py" --export "$EXPORT" --apply
  submit "$p" purchase-submit "$PRIVATE/bexio-purchase-drafts.json"
  importer "$p" payments-in extract_loader python3 "$BEXIO/import_payments_in.py" --export "$EXPORT" --write "$RUNDIR/$p/payments-in.json"
  submit "$p" payments-in-submit "$RUNDIR/$p/payments-in.json"
  importer "$p" payments-out extract_loader python3 "$BEXIO/import_payments_out.py" --export "$EXPORT" --write "$RUNDIR/$p/payments-out.json"
  submit "$p" payments-out-submit "$RUNDIR/$p/payments-out.json"
  importer "$p" vat-fix extract_loader python3 "$BEXIO/import_vat_fix.py" --export "$EXPORT" --write "$RUNDIR/$p/vat-fix.json"
  submit "$p" vat-fix-submit "$RUNDIR/$p/vat-fix.json"
  importer "$p" bank extract_loader python3 "$BEXIO/import_bank.py" --export "$EXPORT" --write "$RUNDIR/$p/bank.json"
  submit "$p" bank-submit "$RUNDIR/$p/bank.json"
  check_step "$p" bank-check - python3 "$BEXIO/import_bank.py" --export "$EXPORT" --check
  importer "$p" manual-entries extract_loader python3 "$BEXIO/import_manual_entries.py" --export "$EXPORT" --write "$RUNDIR/$p/manual-entries.json"
  submit "$p" manual-entries-submit "$RUNDIR/$p/manual-entries.json"
  importer "$p" manual-fix extract_loader python3 "$BEXIO/import_manual_fix.py" --export "$EXPORT" --write "$RUNDIR/$p/manual-fix.json"
  submit "$p" manual-fix-submit "$RUNDIR/$p/manual-fix.json"
  importer "$p" payroll extract_loader python3 "$BEXIO/import_payroll.py" --export "$EXPORT" --write "$RUNDIR/$p/payroll.json"
  submit "$p" payroll-submit "$RUNDIR/$p/payroll.json"
  check_step "$p" payroll-check - python3 "$BEXIO/import_payroll.py" --export "$EXPORT" --check
  importer "$p" opening - python3 "$BEXIO/import_opening.py" --export "$EXPORT" --dry-run
  importer "$p" files extract_files python3 "$BEXIO/import_files.py" --export "$EXPORT" --apply
  importer "$p" document-pdfs extract_pdfs python3 "$BEXIO/import_document_pdfs.py" --export "$EXPORT" --apply
}

# ---- the snapshots: bexio-keyed documents per doctype ----------------------------

snapshot() { # snapshot <name>
  python3 "$BEXIO/delta_counts.py" >"$RUNDIR/$1" || die "delta_counts.py failed"
}

print_snapshot_move() { # print_snapshot_move <before> <after>
  awk -F '\t' 'NR == FNR { before[$1] = $2; next } $1 != "" {
    printf "  %-18s %8d -> %8d\n", $1, before[$1] + 0, $2
  }' "$1" "$2"
}

# ---- the table -------------------------------------------------------------------

sum_counts() { # sum_counts <countsfile...>: sets n c s f and have (1 when any file had numbers)
  n=0
  c=0
  s=0
  f=0
  have=0
  for cf in "$@"; do
    if [ -s "$cf" ]; then
      read -r a b d e <"$cf" || true
      n=$((n + a))
      c=$((c + b))
      s=$((s + d))
      f=$((f + e))
      have=1
    fi
  done
}

print_row() { # print_row <label> <rcfile> <countsfile...>
  label=$1
  rcf=$2
  shift 2
  sum_counts "$@"
  rc="-"
  if [ -f "$rcf" ]; then
    rc=$(cat "$rcf")
  fi
  if [ "$have" = 1 ]; then
    printf '%-16s %6d %8d %8d %7d %5s\n' "$label" "$n" "$c" "$s" "$f" "$rc"
  else
    printf '%-16s %6s %8s %8s %7s %5s\n' "$label" - - - - "$rc"
  fi
}

print_table() { # print_table <pass>
  p=$1
  echo
  echo "counts per step from pass $p (new, changed, skipped, failed) and the step's exit code:"
  echo "step                 new  changed  skipped  failed  exit"
  print_row master "$RUNDIR/$p/master.rc" "$RUNDIR/$p/master.counts"
  if [ "$p" = p2 ]; then
    print_row sales "$RUNDIR/$p/sales.rc" "$RUNDIR/$p/sales.counts" "$RUNDIR/$p/sales-submit.counts"
    print_row "credit note" "$RUNDIR/$p/credit-note.rc" "$RUNDIR/$p/credit-note-submit.counts"
    print_row purchase "$RUNDIR/$p/purchase.rc" "$RUNDIR/$p/purchase.counts" "$RUNDIR/$p/purchase-submit.counts"
    print_row "payments in" "$RUNDIR/$p/payments-in.rc" "$RUNDIR/$p/payments-in-submit.counts"
    print_row "payments out" "$RUNDIR/$p/payments-out.rc" "$RUNDIR/$p/payments-out-submit.counts"
    print_row "vat fix" "$RUNDIR/$p/vat-fix.rc" "$RUNDIR/$p/vat-fix-submit.counts"
    print_row bank "$RUNDIR/$p/bank.rc" "$RUNDIR/$p/bank-submit.counts"
    print_row "bank check" "$RUNDIR/$p/bank-check.rc"
    print_row "manual entries" "$RUNDIR/$p/manual-entries.rc" "$RUNDIR/$p/manual-entries-submit.counts"
    print_row "manual fix" "$RUNDIR/$p/manual-fix.rc" "$RUNDIR/$p/manual-fix-submit.counts"
    print_row payroll "$RUNDIR/$p/payroll.rc" "$RUNDIR/$p/payroll-submit.counts"
    print_row "payroll check" "$RUNDIR/$p/payroll-check.rc"
  else
    print_row sales "$RUNDIR/$p/sales.rc" "$RUNDIR/$p/sales.counts"
    print_row "credit note" "$RUNDIR/$p/credit-note.rc"
    print_row purchase "$RUNDIR/$p/purchase.rc"
    print_row "payments in" "$RUNDIR/$p/payments-in.rc"
    print_row "payments out" "$RUNDIR/$p/payments-out.rc"
    print_row "vat fix" "$RUNDIR/$p/vat-fix.rc"
    print_row bank "$RUNDIR/$p/bank.rc"
    print_row "manual entries" "$RUNDIR/$p/manual-entries.rc"
    print_row "manual fix" "$RUNDIR/$p/manual-fix.rc" "$RUNDIR/$p/manual-fix.counts"
    print_row payroll "$RUNDIR/$p/payroll.rc"
  fi
  print_row opening "$RUNDIR/$p/opening.rc"
  print_row files "$RUNDIR/$p/files.rc" "$RUNDIR/$p/files.counts"
  print_row "document pdfs" "$RUNDIR/$p/document-pdfs.rc" "$RUNDIR/$p/document-pdfs.counts"
  print_row "trial balance" "$RUNDIR/run/trial-balance.rc"
}

# ---- the rerun gates -------------------------------------------------------------

gates() { # the rerun must insert nothing, and the snapshots must not move
  gate_fail=0
  for pair in "p3/master.counts:master" "p3/sales.counts:sales" "p3/files.counts:files" "p3/document-pdfs.counts:document pdfs"; do
    cf="$RUNDIR/${pair%%:*}"
    label=${pair#*:}
    if [ -s "$cf" ]; then
      read -r n1 _ <"$cf" || true
      if [ "${n1:-0}" != "0" ]; then
        echo "delta.sh: the rerun would insert $n1 in $label" >&2
        gate_fail=1
      fi
    fi
  done
  if [ "$gate_fail" = 1 ]; then
    exit 4
  fi
  if ! cmp -s "$RUNDIR/counts-s2" "$RUNDIR/counts-s3"; then
    echo "delta.sh: the rerun moved the bexio-keyed document counts:" >&2
    diff "$RUNDIR/counts-s2" "$RUNDIR/counts-s3" >&2 || true
    exit 5
  fi
}

# ---- the run ---------------------------------------------------------------------

mode=dry
if [ "${DELTA_APPLY:-0}" = 1 ]; then
  mode=apply
fi
echo "delta ($mode) from $EXPORT"
if [ -n "${BACKUP_ID:-}" ]; then
  echo "backup: $BACKUP_ID"
fi
echo "run dir: $RUNDIR"

snapshot counts-s1
chain_dry p1

if [ "$mode" = apply ]; then
  chain_apply
  snapshot counts-s2
  echo
  echo "documents with a bexio_id, before the dry pass -> after the apply pass:"
  print_snapshot_move "$RUNDIR/counts-s1" "$RUNDIR/counts-s2"
  chain_dry p3
  snapshot counts-s3
  gates
else
  snapshot counts-s2
  if ! cmp -s "$RUNDIR/counts-s1" "$RUNDIR/counts-s2"; then
    echo "delta.sh: the dry pass moved the bexio-keyed document counts:" >&2
    diff "$RUNDIR/counts-s1" "$RUNDIR/counts-s2" >&2 || true
    exit 5
  fi
fi

check_step run trial-balance - python3 "$BEXIO/check_trial_balance.py" --export "$EXPORT"

print_table "$([ "$mode" = apply ] && echo p2 || echo p1)"
echo
echo "delta done, run dir: $RUNDIR"
