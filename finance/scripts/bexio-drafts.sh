#!/bin/sh
# Inserts the bexio drafts that the importers hand over into ERPNext site 'frontend': the plan JSON of
# import_sales.py and import_purchase.py with --apply. The loader is bexio-drafts.py; it runs inside the
# backend container with bench's Python, the way swiss-setup.sh runs swiss-setup.py. Its stdin is the plan,
# so the loader's source goes in as an environment variable.
#
#   finance/scripts/bexio-drafts.sh <plan.json> [submit]
#
# Without the second argument the documents stay drafts. With submit, each one is submitted after it is loaded
# (its GL entries are written), in posting date order.
#
# relink sets bexio_id on the submitted Journal Entries an earlier run loaded without one (see relink in the loader);
# check does the same matching and writes nothing.
set -eu

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

plan="${1:?usage: bexio-drafts.sh <plan.json> [draft|submit|relink|check]}"
mode="${2:-draft}"
case "$mode" in draft | submit | relink | check) ;; *)
  echo "mode is draft, submit, relink or check, not $mode" >&2
  exit 2
  ;;
esac

# A draft load is part of the hourly sync, so its short lease must not keep the VM up all day.
COLIMA_WAKE=${COLIMA_WAKE:-/Users/bsaladin/ws_yardr/integrations/bin/colima-wake}
if [ -x "$COLIMA_WAKE" ]; then
  case "$mode" in
  submit | relink) HOLD=60 ;;
  draft | check) HOLD=15 ;;
  esac
  if ! "$COLIMA_WAKE" --hold "$HOLD" --reason bexio-drafts; then
    echo "bexio-drafts.sh: colima-wake failed; the VM is not up, the run cannot work without it" >&2
    exit 1
  fi
fi
LOADER=$(cat finance/scripts/bexio-drafts.py)
export LOADER

docker compose -p frappe-finance -f pwd.yml -f finance-local.yml exec -T -e LOADER -e MODE="$mode" backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python -c "$LOADER"' \
  <"$plan"
