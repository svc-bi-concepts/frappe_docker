#!/bin/sh
# The host part of the finance gate: the check lines that need no private/ data and no custom
# image. .yardr/check (the yard's merge gate) and .github/workflows/finance-tests.yml (GitHub
# CI) both call this file, so the compose checks and the test-module lists exist once and
# cannot drift apart. Run from the repository root, with a python3 that has jinja2 (only
# test_payslip needs it).
# The guard: every test_*.py under bi_finance, bi_payroll and finance/scripts must be named
# in this file or in .yardr/check (the guard reads both, the gate is the two together), so a
# new module that is in no list fails the gate here instead of waiting unseen on main (IT's
# standing rule, 2026-10-15). The stdlib-only modules are named on the host lines below, the
# frappe-image modules in the docker run line of .yardr/check; no list names a module twice.
# A module counts as named only as its whole dotted name on a code line (comments are not
# read): bi_finance.test_treasury hides neither behind bi_finance.test_treasury_card nor
# behind bi_payroll's test_layout (review, erp-evgh).
# SC2155: the split export keeps mktemp's failure visible the same as before (both run before set -e).
export PYTHONDONTWRITEBYTECODE=1
PYTHONPYCACHEPREFIX="$(mktemp -d)"
export PYTHONPYCACHEPREFIX
set -e
python3 - "$0" "$(dirname "$0")/check" <<'EOF'
import pathlib, re, sys
gate = "\n".join(l for a in sys.argv[1:] for l in pathlib.Path(a).read_text().splitlines() if not l.lstrip().startswith("#"))
roots = {"finance/apps/bi_finance/bi_finance": "bi_finance.", "finance/apps/bi_payroll/bi_payroll": "bi_payroll.", "finance/scripts": ""}
missing = [str(p) for root, pkg in roots.items() for p in sorted(pathlib.Path(root).glob("test_*.py")) if not re.search(rf"\b{re.escape(pkg + p.stem)}\b", gate)]
if missing:
    sys.exit("test modules in no gate list:\n  " + "\n  ".join(missing))
EOF
# The finance stack's compose files (live and the HRMS copy) must parse together.
docker compose -f pwd.yml -f finance-local.yml config -q
docker compose -f pwd.yml -f finance-copy.yml config -q
# The copy stack is started on demand: no service in it restarts by itself after a reboot.
docker compose -f pwd.yml -f finance-copy.yml config --format json | python3 -c "
import json, sys
s = json.load(sys.stdin)['services']
assert all(v.get('restart') != 'unless-stopped' for v in s.values()), [n for n, v in s.items() if v.get('restart') == 'unless-stopped']
"
# The long-running services come back after a VM restart (restart: unless-stopped); the one-shot ones do not.
docker compose -f pwd.yml -f finance-local.yml config --format json | python3 -c "
import json, sys
s = json.load(sys.stdin)['services']
long = ['backend', 'db', 'frontend', 'queue-long', 'queue-short', 'redis-cache', 'redis-queue', 'scheduler', 'websocket']
assert all(s[n].get('restart') == 'unless-stopped' for n in long), [n for n in long if s[n].get('restart') != 'unless-stopped']
assert all(s[n].get('restart') != 'unless-stopped' for n in ['configurator', 'create-site'])
"
# The bi_finance layout and stamp guard, stdlib only (no frappe), so it runs on the host.
# A synced JSON changed without a newer 'modified' fails here; see test_layout.py.
# test_assistant, test_assistant_levels, test_assistant_actions and test_assistant_ledger_actions run on the host too: their frappe is a stub in the module itself.
(cd finance/apps/bi_finance && python3 -m unittest bi_finance.test_layout bi_finance.test_wise_client bi_finance.test_estv_vat_xml bi_finance.test_mwst_payment bi_finance.test_treasury_card bi_finance.test_vat_declaration_client bi_finance.test_bank_match bi_finance.test_camt bi_finance.test_mwst_quarterly bi_finance.test_sidebar bi_finance.test_erpnextswiss_desk_records bi_finance.test_module_profile_finance bi_finance.test_cash_conversion bi_finance.test_cash_forecast bi_finance.test_assistant bi_finance.test_assistant_levels bi_finance.test_assistant_actions bi_finance.test_assistant_ledger_actions)
# The bi_payroll formulas (invented employees), the ESTV withholding tariffs (invented rows), the payroll hand-over rows
# (invented employees), the payslip data and the Lohnabrechnung template's render (invented employee), the Quellensteuer
# tariff codes (invented rows) and its layout and stamp guard, stdlib only (test_payslip's render needs jinja2).
(cd finance/apps/bi_payroll && python3 -m unittest bi_payroll.test_swiss_payroll bi_payroll.test_quellensteuer bi_payroll.test_hand_over bi_payroll.test_structure_gap bi_payroll.test_qst_codes bi_payroll.test_payslip bi_payroll.test_layout bi_payroll.test_sidebar bi_payroll.test_payroll_accounts)
# The bexio importers' unit tests, stdlib only, about half a second.
(cd finance/bexio && python3 -m unittest discover -s . -p "test_*.py")
# The SharePoint reader's tests on a fake Graph (no network), with openssl for the signing check.
(cd finance/sharepoint && python3 -m unittest discover -s . -p "test_*.py")
# The mailbox tool's tests on a stubbed HTTP layer (invented mail, no network), with openssl for the assertion check.
(cd finance/mail && python3 -m unittest discover -s . -p "test_*.py")
# The image app lists (live without hrms, copy with it) and build-image.sh's modes; no docker.
# switch-image.sh's decisions (which tag runs, which images go at rest) with stub docker and curl.
# gate-image.sh's choice of the gate's image (main's pin, the branch's, the one at rest) with stub git and docker.
# The copy stack's guards (live project and missing image refused) with stub docker, and its compose file's restarts and ports.
# The D4 patch check (update-check.sh): invented tag lists, the upstream replaced, no network.
# swiss-setup.py's pure helpers (frappe mocked): the payslip and the hr steps among them.
# The structure remake's plan (frappe mocked): create, move, restore, the wrapper's project.
(cd finance/scripts && python3 -m unittest test_build_image test_switch_image test_gate_image test_copy_stack test_update_check test_load_swiss_rates test_swiss_setup test_swiss_structure_remake test_bexio_drafts test_swiss_vat)
