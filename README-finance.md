# Finance ERPNext stack

Operator guide for the finance yard's ERPNext stack. The upstream README.md
still describes the generic setup; this file covers only what this yard runs.

## What runs

- The local Compose project `frappe-finance` uses `pwd.yml` (the ERPNext stack)
  and `finance-local.yml` (the loopback-only port override). The separate
  `finance-coolify.yml` file is a self-contained stack for a Coolify VM; use it
  alone, with required `LEDGER_HOST`, `DB_ROOT_PASSWORD`, and `ADMIN_PASSWORD`
  environment variables. Its image must be loaded locally and its tag is kept
  aligned with `finance-local.yml` by the image switch script.
- ERPNext `v16.50.0` / frappe `v16.51.1` with erpnextswiss, site `frontend` (the default
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

For the Coolify stack, use `docker compose -f finance-coolify.yml` on its VM
after loading the image. To check public reachability, run
`finance/scripts/public-check.sh` from outside the VM's private network. It
reads `private/public-check-hosts` (mode 600); see the script's usage comment
for the hosts file format and check outcomes. The Coolify compose file documents
its network and router constraints.

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

Scripts and agents call the API as one of three users, never as
`Administrator`. Each is a System User with no login password and its own key
file (mode 600, lines `url=`, `api_key=`, `api_secret=`):

| User                      | Key file                               | Used by                            | Roles                                                                                                                                    |
| ------------------------- | -------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `api-agent@finance.local` | `~/ws_yardr_finance/.erpnext-api`      | the importers                      | Accounts, Sales, Purchase and Stock User, the Sales and Purchase Master Managers, Item Manager, HR User; System Manager stays (erp-wnx7) |
| `api-sync@finance.local`  | `~/ws_yardr_finance/.erpnext-api-sync` | the hourly sync (`sync_hourly.sh`) | the agent's list without HR User                                                                                                         |
| `api-mcp@finance.local`   | `~/ws_yardr_finance/.erpnext-api-mcp`  | every MCP session                  | Accounts, Sales, Purchase and Stock User                                                                                                 |

The MCP user holds no payroll role (HR User reads Employee and Salary Slip) and
no System Manager: what an MCP session reads goes to the model provider, so
payroll must not be reachable through it. The sync and mcp lists are exact, a
provisioning run removes any other role, and the script refuses to run if
either list holds System Manager, HR User, HR Manager or AI Approver.
`assistant_actions.API_USERS` names all three, so none of them approves an AI
Action. An agent with a shell can still read `.erpnext-api` or use `docker exec`;
the split closes the MCP path only. `python3 finance/bexio/api_key_check.py
<key file>` asks the doctypes of every hrms module and BI Payroll, Employee,
Deleted Document, Version, and File on Salary Slip and on Employee for one row
each, and says for each whether the key is refused, sees a row, or may list an
empty doctype. It exits 0 only when every line is refused (File lines may be
empty).

Until a key file exists, its consumer falls back: `sync_hourly.sh` to
`.erpnext-api`, `run-frappe-mcp.sh` to `.erpnext-api` with one warning on stderr.

- Base URLs:
  - https://bsaladins-mac-mini-1.tail61fee9.ts.net:8448/api/ (tailnet)
  - http://127.0.0.1:8080/api/ (on the mini; IT's front door wakes the VM and refreshes its idle clock)
- Header: `Authorization: token <key>:<secret>`
- Credentials: the key files above. Read them from there; never copy them into
  the repo, a commit or a note.
- Rerun the provisioning script from the repo root. It is idempotent: a second
  run changes nothing.

  ```sh
  finance/scripts/create-api-user.sh            # create or repair api-agent
  finance/scripts/create-api-user.sh --rotate   # also issue a new key and secret
  finance/scripts/create-api-user.sh --user sync   # api-sync, keys in .erpnext-api-sync
  finance/scripts/create-api-user.sh --user mcp    # api-mcp, keys in .erpnext-api-mcp
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
