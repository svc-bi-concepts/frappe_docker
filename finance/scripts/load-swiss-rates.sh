#!/bin/sh
# The rates from the private CSV into Payroll Swiss Settings (load_swiss_rates.py).
# The CSV never enters the container: its set=yes rows go through RATES_JSON, like
# swiss-setup.sh passes the export files. Dry run on the copy by default; the live
# stack only with --live, after the go on the bead (backup first, one writer).
#
#   finance/scripts/load-swiss-rates.sh                 # dry run on the copy: what would change
#   finance/scripts/load-swiss-rates.sh --apply         # set the fields on the copy
#   finance/scripts/load-swiss-rates.sh --apply --live --backup   # the live stack, with a backup first
#
# RATES_CSV overrides the CSV path (default: private/payroll-insurance/rates-2026.csv).
set -eu

cd "$(dirname "$0")/../.."

CSV="${RATES_CSV:-/Users/bsaladin/ws_yardr_finance/private/payroll-insurance/rates-2026.csv}"
[ -f "$CSV" ] || { echo "no rates CSV at $CSV" >&2; exit 1; }

APPLY="" LIVE="" BACKUP=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --live) LIVE=1 ;;
    --backup) BACKUP=1 ;;
    *) echo "unknown argument $arg" >&2; exit 2 ;;
  esac
done
[ -z "$LIVE" ] || [ -n "$APPLY" ] || { echo "--live needs --apply" >&2; exit 2; }

if [ -n "$LIVE" ]; then
  PROJECT="frappe-finance"
  FILES="-f pwd.yml -f finance-local.yml"
else
  # The copy is the default: a builder's dry run must never touch the live stack.
  PROJECT="frappe-finance-copy"
  FILES="-f pwd.yml -f finance-copy.yml"
fi

RATES_JSON=$(python3 finance/scripts/load_swiss_rates.py --prepare "$CSV")
export RATES_JSON

if [ -n "$BACKUP" ]; then
  docker compose -p "$PROJECT" $FILES exec -T backend \
    bench --site frontend backup --with-files
fi

MODE=--dry
if [ -n "$APPLY" ]; then
  MODE=--apply
fi

docker compose -p "$PROJECT" $FILES exec -T -e RATES_JSON backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python - "$@"' sh "$MODE" \
  < finance/scripts/load_swiss_rates.py
