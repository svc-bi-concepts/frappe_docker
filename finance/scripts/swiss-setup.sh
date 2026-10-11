#!/bin/sh
# Swiss setup of company BI Concepts on site 'frontend': KMU chart of
# accounts, VAT templates, fiscal years and the bexio_id fields. Steps run in
# that order and are re-runnable. Needs the swiss image with erpnextswiss
# installed (finance/docs/erpnext-setup.md).
#
#   finance/scripts/swiss-setup.sh                # all steps
#   finance/scripts/swiss-setup.sh vat fiscal     # some of: coa accounts vat fiscal fields currencies banks gebuev qrbill host payments hr payroll_accounts lohnausweis expenses (HR 11 expenses: the claim route as the optional route for employee expenses — the setup step 'expenses' (five Expense Claim Types on bexio's 5811, 5820, 5821, 5822, 5832, the Company's default expense-claim payable 2003, the Expense Approver role on HR_USER), the two routes in operating.md (claim with VAT through the Payment Proposal run, or flat allowances by Additional Salary with the payroll), Payment Entries for claim rows in the payment run (paid from the UBS account, to the claim's payable, submitted on download), the erpnextswiss patches 0002 (the proposal's claim rows pay grand_total, so a VAT claim is not paid short) and 0003 (get_primary_address called .format instead of formatting, every Payment Proposal was refused), offline tests (erp-j9kn))
#   finance/scripts/swiss-setup.sh vat --check <export dir>   # VAT codes against the export's taxes.json
#   finance/scripts/swiss-setup.sh hr --check     # read-only: HR roles of enabled users and the two settings
#   HR_USER=<user> finance/scripts/swiss-setup.sh hr          # the HR roles on that user and the HR settings
#   finance/scripts/swiss-setup.sh freeze 2025-12-31          # dry run: what a freeze would set
#   finance/scripts/swiss-setup.sh freeze 2025-12-31 --apply  # sets accounts frozen till that date
#
# host sets the site's host_name from HOST_NAME (the URL the backend can reach, e.g.
# https://<machine>.<tailnet>.ts.net:8448); it is skipped when HOST_NAME is empty.
# expenses sets the Expense Approver role on the user in HR_USER (Benchi); skipped when empty.
set -eu

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# The container cannot see the export directory: its taxes.json and
# accounts.json go in as environment variables.
TAXES_JSON="" ACCOUNTS_JSON=""
if [ "${1:-}" = vat ] && [ "${2:-}" = --check ]; then
  dir="${3:?usage: swiss-setup.sh vat --check <export dir>}"
  TAXES_JSON=$(cat "$dir/taxes.json")
  ACCOUNTS_JSON=$(cat "$dir/accounts.json")
fi
export TAXES_JSON ACCOUNTS_JSON

# bench's own Python loads the site; the script goes in on stdin so the
# container needs no copy of it.
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml exec -T \
  -e BANKS="${BANKS:-}" -e HOST_NAME="${HOST_NAME:-}" -e HR_USER="${HR_USER:-}" \
  -e TAXES_JSON -e ACCOUNTS_JSON backend \ (HR 11 expenses: the claim route as the optional route for employee expenses — the setup step 'expenses' (five Expense Claim Types on bexio's 5811, 5820, 5821, 5822, 5832, the Company's default expense-claim payable 2003, the Expense Approver role on HR_USER), the two routes in operating.md (claim with VAT through the Payment Proposal run, or flat allowances by Additional Salary with the payroll), Payment Entries for claim rows in the payment run (paid from the UBS account, to the claim's payable, submitted on download), the erpnextswiss patches 0002 (the proposal's claim rows pay grand_total, so a VAT claim is not paid short) and 0003 (get_primary_address called .format instead of formatting, every Payment Proposal was refused), offline tests (erp-j9kn))
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python - "$@"' sh "$@" \
  < finance/scripts/swiss-setup.py
