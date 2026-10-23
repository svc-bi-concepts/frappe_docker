# Finance ERPNext stack

Operator guide for the finance yard's ERPNext stack. The upstream README.md
still describes the generic setup; this file covers only what this yard runs.

## What runs

- Compose project `frappe-finance`, built from two files:
  `pwd.yml` (the ERPNext stack) and `finance-local.yml` (the loopback-only
  port override).
- ERPNext / frappe `v16.50.0` with erpnextswiss, site `frontend` (the default
  site). The image tag is set in `finance-local.yml`; build an image with
  `finance/scripts/build-image.sh live <tag>`. Swiss
  setup of the company: `finance/docs/erpnext-setup.md`. The recurring work (month, quarter,
  payroll) in Desk order: `finance/docs/operating.md`.
- Services: `backend`, `frontend`, `websocket`, `scheduler`, `queue-short`,
  `queue-long`, `configurator`, `create-site`, `db` (MariaDB 11.8),
  `redis-cache` and `redis-queue`.

## Start, stop, status, logs

Always pass `-p frappe-finance`. Without it, compose names the project after
the directory it runs in, so you get a second, empty stack.

```sh
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml down
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml ps
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml logs -f <service>
```

`down` keeps the data volumes (`db-data`, `sites`, and so on). Adding `-v`
deletes them, so the site and its data are lost.

## Reaching it

- http://127.0.0.1:8082, bound to loopback only (`finance-local.yml` replaces
  the upstream port mapping with `127.0.0.1:8082:8080`). 8080 is the colima
  front door's host port, so ERPNext moved to 8082 (container port unchanged).
- From the tailnet: https://bsaladins-mac-mini-1.tail61fee9.ts.net:8448 (tailnet
  only, not public). `tailscale serve` on the mini proxies it to 127.0.0.1:8082.
- To check or recreate the mapping on the mini, use the Homebrew binary:
  `/opt/homebrew/bin/tailscale serve status`. The `tailscale` wrapper in
  `~/.local/bin` points to a missing app, so do not use it.
- On the mini itself, use the loopback URL http://127.0.0.1:8082.
- Do not open the port on the LAN.

## Login

- User: `Administrator`.
- The password reference is `~/ws_yardr_finance/.erpnext-admin` (mode 600); read
  the current password from there. The `create-site` command in `pwd.yml` sets
  the initial password to `admin`. The file is authoritative only if it was
  updated to match the site (for example, after changing the password). Never
  copy a password into the repo, a commit, or a note.

## API access

Scripts and agents call the API as `api-agent@finance.local`, not as
`Administrator`. The user is a System User with no login password, and has the
roles Accounts User, Sales User, Purchase User and Stock User.

- Base URLs:
  - https://bsaladins-mac-mini-1.tail61fee9.ts.net:8448/api/ (tailnet)
  - http://127.0.0.1:8082/api/ (on the mini)
- Header: `Authorization: token <key>:<secret>`
- Credentials: `~/ws_yardr_finance/.erpnext-api` (mode 600, lines `url=`,
  `api_key=`, `api_secret=`). Read them from there; never copy them into the
  repo, a commit or a note.
- Rerun the provisioning script from the repo root. It is idempotent: a second
  run changes nothing.

  ```sh
  finance/scripts/create-api-user.sh            # create or repair the user
  finance/scripts/create-api-user.sh --rotate   # also issue a new key and secret
  ```

  The script also issues keys when the keys file is missing, because the
  secret cannot be read back from the site. Neither value is printed.

## Remotes

- `origin`: `github.com/svc-bi-concepts/frappe_docker` (our fork, base `main`).
- `upstream`: `github.com/frappe/frappe_docker`.
- To pull upstream changes:

  ```sh
  git fetch upstream && git merge upstream/main
  ```

## The gate

`.yardr/check` is the merge gate for this depot. It runs the host checks in
`.yardr/check-host.sh`, the finance tests that need the custom image, and upstream's
pre-commit lint when `pre-commit` is installed. The host checks validate both
compose configurations and run the tests that do not need the image.

Before calling Docker, `.yardr/check` checks that the physical checkout path is
under `$HOME`. Colima on the Mac mini mounts only the home folder, so an outside
checkout would otherwise appear empty in the container. Set `GATE_ANY_PATH=1` to
skip this guard on a host whose Docker can access other paths.

Not covered here: backups and upgrades. Those come in later beads.

## Assistant (read part)

The Assistant is a chat page on ERPNext's rail, next to Treasury. It answers "what is open
for me" from fixed read-only collectors that run as the logged-in user (their own
permissions decide what they see; counts and the first ten each) and sends the question
plus that result to OpenRouter. Nothing is written; the answer comes back as text plus
links (`/app/<doctype>/<name>`). Requests are routed only to OpenRouter endpoints that
keep nothing (zero data retention, no training, no fallback); the key lives in
`AI Assistant Settings` (Password field), is never logged and never reaches the browser.
Payroll results carry counts and Salary Slip names, Expense Claims counts, claim names and
amounts; never the employee. While today is before `AI Assistant Settings`' ledger date (`ledger_actions_from`,
default 2027-01-01) every answer starts with the line that bexio leads until that date. The
collector keys and what each returns: the module docstring and each collector's docstring.

Switching it on: enter the OpenRouter API key in `AI Assistant Settings`, tick Enabled.
The page says what is missing when the assistant is off or the key is absent. Code:
`finance/apps/bi_finance/bi_finance/assistant.py` (collectors, one registry) and
`assistant_client.py` (the OpenRouter call); offline tests:
`python3 -m unittest bi_finance.test_assistant`.

## Payslip (Lohnabrechnung)

The payslip is bi_payroll's print format `Lohnabrechnung` on Salary Slip: a generic German A4
sheet (header with company, employee, masked AHV number and period; earnings; deductions with
base and rate where the formula has one; net pay and the paid-out amount with the bank account's
last digits; the employer's shares in a separate information block; year-to-date gross and net).
No company data is in the repo: company and employee come from the records at print time, and
the rows come from the slip's own tables and the rates copied onto it
(`finance/apps/bi_payroll/bi_payroll/payslip.py`, prepared for the template
`lohnabrechnung.html` through the jinja method `payslip_data`).

Installed by `swiss-setup.sh payslip` (creates or updates the format from the app's file and
makes it the Salary Slip default with a Property Setter; re-runnable). The step runs live only
in the go-live bead after the HR train lands, with backup, one writer and ledgerdemain's go.
Offline: `python3 -m unittest bi_payroll.test_payslip` renders and checks the data with an
invented employee; the numbers of a printed slip must equal the slip's own.
