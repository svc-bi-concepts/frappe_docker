#!/bin/sh
# Runs baked image tests without a site, database, network or checkout mount.
set -eu
SCRIPTS=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
BENCH=$(CDPATH='' cd -- "$SCRIPTS/../.." && pwd)
# shellcheck source=finance/scripts/gate-modules.sh
. "$SCRIPTS/gate-modules.sh"
[ "$#" -gt 0 ] || {
  echo "gate-tests: empty module list" >&2
  exit 2
}
# frappe's logger writes relative to the app directory when ERPNext is imported.
mkdir -p "$BENCH/../logs" "$BENCH/apps/logs"
cd "$BENCH/apps/bi_finance"
status=0
"$BENCH/env/bin/python" -m unittest "$@" || status=$?
if [ "$status" -eq 0 ]; then
  echo "gate-tests: PASS ($# modules)"
else
  echo "gate-tests: FAIL ($# modules, exit $status)"
fi
exit "$status"
