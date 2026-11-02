#!/bin/sh
# Read-only comparison reports for a finance site and a restored copy (finance/docs/archiving.md, 'Checks').
#
# usage: finance/scripts/site-check.sh report <name>       write private/site-check/<name>.txt, print one sha256 per section
#        finance/scripts/site-check.sh compare <a> <b>     two reports: equal, or the differing lines; exit 0 when equal, 1 when not
#        finance/scripts/site-check.sh digests <a> <b>     two files of saved report output; exit 0 when equal, 1 when not
#        FINANCE_PROJECT=frappe-finance-copy finance/scripts/site-check.sh report <name>   the copy; the default is live
set -eu

PROJECT="${FINANCE_PROJECT:-frappe-finance}"
PRIVATE="${FINANCE_PRIVATE:-/Users/bsaladin/ws_yardr_finance/private}"
ROOT=$(CDPATH='' cd -- "$(dirname "$0")/../.." && pwd)
ACTION="${1:-}"

case "$PROJECT" in
frappe-finance | frappe-finance-copy*) ;;
*)
  echo "FINANCE_PROJECT must be frappe-finance or frappe-finance-copy*" >&2
  exit 2
  ;;
esac

valid_name() {
  case "$1" in
  '' | . | .. | *[!A-Za-z0-9._-]*) return 1 ;;
  *) return 0 ;;
  esac
}

case "$ACTION" in
report)
  [ "$#" -eq 2 ] || {
    echo "usage: site-check.sh report <name>" >&2
    exit 2
  }
  NAME=$2
  valid_name "$NAME" || {
    echo "invalid report name" >&2
    exit 2
  }
  DIR="$PRIVATE/site-check"
  mkdir -p "$DIR"
  chmod 700 "$DIR"
  TARGET="$DIR/$NAME.txt"
  TMP=$(mktemp "$DIR/.site-check.XXXXXX")
  trap 'rm -f "$TMP"' EXIT HUP INT TERM
  chmod 600 "$TMP"
  docker compose -p "$PROJECT" -f "$ROOT/pwd.yml" -f "$ROOT/finance-local.yml" exec -T \
    backend sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python -' \
    <"$ROOT/finance/scripts/site-check.py" >"$TMP"
  chmod 600 "$TMP"
  mv -f "$TMP" "$TARGET"
  trap - EXIT HUP INT TERM
  python3 "$ROOT/finance/scripts/site-check.py" digest-report "$TARGET"
  ;;
compare)
  [ "$#" -eq 3 ] || {
    echo "usage: site-check.sh compare <a> <b>" >&2
    exit 2
  }
  if ! valid_name "$2" || ! valid_name "$3"; then
    echo "invalid report name" >&2
    exit 2
  fi
  python3 "$ROOT/finance/scripts/site-check.py" compare "$PRIVATE/site-check/$2.txt" "$PRIVATE/site-check/$3.txt"
  ;;
digests)
  [ "$#" -eq 3 ] || {
    echo "usage: site-check.sh digests <a> <b>" >&2
    exit 2
  }
  python3 "$ROOT/finance/scripts/site-check.py" digests "$2" "$3"
  ;;
*)
  echo "usage: site-check.sh report <name> | compare <a> <b> | digests <a> <b>" >&2
  exit 2
  ;;
esac
