#!/bin/sh
# One-shot, idempotent remake of the Swiss Monthly salary structure (the HR 1
# decision of 2026-10-11: the six employer shares are Employer Contribution
# components). Pipes swiss-structure-remake.py into a backend container, the
# way swiss-setup.sh does; every step is skipped when its end state holds, so
# the rerun changes nothing. FINANCE_PROJECT rehearses on the copy stack:
#
#   finance/scripts/swiss-structure-remake.sh                          # live
#   FINANCE_PROJECT=frappe-finance-copy finance/scripts/swiss-structure-remake.sh
set -eu

PROJECT="${FINANCE_PROJECT:-frappe-finance}"

cd "$(dirname "$0")/../.."

docker compose -p "$PROJECT" -f pwd.yml -f finance-local.yml exec -T \
  backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python -' \
  < finance/scripts/swiss-structure-remake.py
