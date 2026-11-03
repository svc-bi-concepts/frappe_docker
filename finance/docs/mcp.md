# ERPNext MCP server

Claude Code reads and writes the finance ERPNext site through an MCP server.
This page records which server we evaluated, why we picked it, how it runs,
and the read test that proves it works.

## Pick

**`frappe-mcp-server` 1.2.0**, pinned in `finance/mcp/requirements.txt`, run
from `finance/mcp/venv` by `finance/mcp/run-frappe-mcp.sh`.

Why this one:

- MIT, one small package, two runtime dependencies (`mcp`, `httpx`).
- Talks only to the ERPNext REST API (`/api/resource`, `/api/method`) at the
  URL it is given. No bench or shell access, no telemetry, no other hosts.
- Authenticates with the API key and secret of `api-mcp@finance.local`, the
  dedicated user from `finance/scripts/create-api-user.sh --user mcp`. It holds
  exactly Accounts, Sales, Purchase and Stock User: no HR User or HR Manager,
  no System Manager, no AI Approver (the script refuses them). Everything a
  session reads goes to the model provider, so payroll, HR and Employee data
  must stay out of its reach, see "What the keys reach" below. The importers
  keep `api-agent`, the hourly sync uses `api-sync` (the same four roles plus
  Sales and Purchase Master Manager and Item Manager); the three key files are
  listed in `README-finance.md`.
- Generic CRUD and a method call, so it needs no code in the ERPNext site.

## Evaluation

Checked 2026-10-09. Versions and dates are from PyPI and GitHub.

| Candidate                            | Source, licence                                             | Last release / activity                         | Works with v16                           | Tools                                                                                  | Auth                                           | Verdict                                                                           |
| ------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------- |
| `frappe-mcp-server` 1.2.0 (PyPI)     | github.com/muthanii/frappe_mcp, MIT                         | 1.2.0 on 2026-07-29; repo pushed 2026-07-29     | Yes: tested against v16.50.0 (see below) | ping, get_doc, search_docs, create_doc, update_doc, delete_doc, run_method             | `Authorization: token key:secret` from env     | **Picked**                                                                        |
| `erpnext-agent` 1.0.1 (PyPI)         | github.com/Knuckles-Team/erpnext-agent, MIT                 | 1.0.1 on 2026-07-02; repo pushed 2026-10-08     | Not tested                               | CRUD, call_method, get_logged_user, plus a generic `erpnext_agent_authentication` tool | token / Bearer / session via its own auth tool | Not picked, see below                                                             |
| `frappe/mcp` (github.com/frappe/mcp) | Frappe org, MIT, 169 stars                                  | pushed 2026-05-29; marked "highly experimental" | Not tested                               | tools only                                                                             | OAuth2 (needs frappe OAuth updates)            | Not picked: it is a library that runs inside a Frappe app, so it changes the site |
| `ManotLuijiu/erpnext_mcp_server`     | github.com/ManotLuijiu/erpnext_mcp_server, MIT, 4 stars     | pushed 2026-10-05                               | Not tested                               | file ops, read-only DB, API                                                            | ERPNext API                                    | Not picked: a Frappe app inside the site (brief rule); not code-reviewed          |
| `sajmustafake/frappe-dev-mcp-server` | github.com/SajmustafaKe/frappe-dev-mcp-server, MIT, 4 stars | pushed 2026-06-11                               | Not tested                               | DocType creation, bench commands, app management                                       | none needed                                    | Out: development tool with bench access (brief rule); not code-reviewed           |
| `superWorldSavior/mcp-erpnext`       | github.com/superWorldSavior/mcp-erpnext, MIT, 103 stars     | pushed 2026-10-05                               | Not tested                               | not reviewed                                                                           | not reviewed                                   | Not evaluated: found by search, not on the brief's list                           |

### Why not `erpnext-agent`

It is a sound project, but it carries more risk than the job needs:

- It depends on `agent-utilities`, a separate package with its own dependency
  tree. We did not review that tree.
- Its `erpnext_agent_authentication` tool takes an `action` string from the
  caller and calls `getattr(client, action)` on the API client with
  caller-supplied keyword arguments. Any method of the client can be reached
  that way, and `login` takes credentials as tool arguments.
- The client calls `urllib3.disable_warnings(InsecureRequestWarning)`, which
  hides TLS warnings.

### Why `frappe/mcp` is out

The brief says there is no MCP server from the Frappe team. That is not quite
right: `github.com/frappe/mcp` exists (MIT, last push 2026-05-29). It is not a
standalone server, though. Its README says a Frappe app serves MCP itself
(`@mcp.register()` in `app/mcp.py`), so using it means installing an app into
the finance site. The brief rules that out, so we did not install it.

## What we read

We read the code of `frappe-mcp-server` 1.2.0 from its PyPI wheel and sdist:
`frappe_mcp/client.py`, `frappe_mcp/server.py`, and the metadata. We did not
diff it against the GitHub tag, and we did not run its own test suite.

- `client.py`: every URL segment is percent-encoded (`_seg`), so a tool argument
  cannot escape its endpoint. The only host it contacts is `FRAPPE_URL`.
- `server.py`: reads `FRAPPE_URL`, `FRAPPE_API_KEY`, `FRAPPE_API_SECRET`, and
  optionally `FRAPPE_VERIFY_SSL` and `FRAPPE_TIMEOUT`. Logs go to stderr, so
  stdio stays clean. It exposes seven tools (listed in the test below).
- `frappe_run_method` calls `/api/method/<dotted path>`. What it can reach is
  limited by the method gate (the name must be on `ALLOWED_METHODS`, see "What
  the keys reach" below) and by the MCP user's roles (listed above; the table
  under "API access" in `README-finance.md` owns the three users' role lists),
  not System Manager.

## How it runs

- `finance/mcp/requirements.txt` pins `frappe-mcp-server==1.2.0`.
- `finance/mcp/venv` is created with `uv venv` and filled with
  `uv pip install -r finance/mcp/requirements.txt`. It is git-ignored (the
  `venv` pattern in `.gitignore`).
- `finance/mcp/run-frappe-mcp.sh` reads `url=`, `api_key=` and `api_secret=`
  from `~/ws_yardr_finance/.erpnext-api-mcp` at each start. While that file is
  missing (before keys split 2) it reads `.erpnext-api` instead and prints one
  warning on stderr; the fallback is removed once the split has passed. It
  strips the `/api/` suffix from the URL, exports the three variables, and
  execs the server. The secret never goes into the repo, into argv, or into
  the output.
- `finance/mcp/mcp.example.json` is the Claude Code config. Copy its `erpnext`
  entry into your own config and set the absolute path to the wrapper.

## Read test

Read-only: nothing was created, updated or deleted in ERPNext. The inline
client below starts the wrapper over stdio and calls the server.

Repeatable command (requires the pinned venv and the private token file):

```sh
finance/mcp/venv/bin/python - <<'PY'
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command='finance/mcp/run-frappe-mcp.sh', args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print('tools:', [tool.name for tool in tools.tools])
            calls = [
                ('frappe_ping', {}),
                ('frappe_search_docs', {'doctype': 'Customer', 'fields': ['name'], 'limit': 1}),
                ('frappe_search_docs', {'doctype': 'Item', 'fields': ['name', 'item_name'], 'limit': 1}),
            ]
            for name, args in calls:
                result = await session.call_tool(name, args)
                print(f'### {name} {json.dumps(args)}')
                for block in result.content:
                    print(block.text)
            result = await session.call_tool('frappe_get_doc', {'doctype': 'Item', 'name': 'SKU008'})
            print('### frappe_get_doc {"doctype": "Item", "name": "SKU008"}')
            print(result.content[0].text[:700] + ' ...')
            result = await session.call_tool('frappe_run_method', {'method': 'frappe.auth.get_logged_user'})
            print('### frappe_run_method {"method": "frappe.auth.get_logged_user"}')
            print(result.content[0].text)

asyncio.run(main())
PY
```

Output, trimmed (the Item record is cut to its first fields):

```text
tools: ['frappe_ping', 'frappe_get_doc', 'frappe_search_docs', 'frappe_create_doc', 'frappe_update_doc', 'frappe_delete_doc', 'frappe_run_method']

### frappe_ping {}
{
  "message": "pong"
}

### frappe_search_docs {"doctype": "Customer", "fields": ["name"], "limit": 1}
{
  "data": [
    {
      "name": "Grant Plastics Ltd."
    }
  ]
}

### frappe_search_docs {"doctype": "Item", "fields": ["name", "item_name"], "limit": 1}
{
  "data": [
    {
      "name": "SKU008",
      "item_name": "Backpack"
    }
  ]
}

### frappe_get_doc {"doctype": "Item", "name": "SKU008"}
{
  "data": {
    "name": "SKU008",
    "owner": "Administrator",
    "item_code": "SKU008",
    "item_name": "Backpack",
    "item_group": "Demo Item Group",
    "stock_uom": "Nos",
    ...
  }
}

### frappe_run_method {"method": "frappe.auth.get_logged_user"}
{
  "message": "api-agent@finance.local"
}
```

Results:

- Customer: 1 listed (`Grant Plastics Ltd.`).
- Item: 1 read (`SKU008`, Backpack). It was created by `Administrator` on
  2026-10-08, before this bead; we did not create it.
- Identity: the server called the site as `api-agent@finance.local`, not
  `Administrator` (the evaluation ran before the split; sessions run as
  `api-mcp@finance.local` once its key file exists).

The server's `frappe_run_method` and `frappe_get_doc` reach the same data the
MCP user may see; the write tools were not exercised.

## What the keys reach

The MCP key and the sync key must read no payroll, HR or Employee data. Their
roles are the first half of that; the rest is what those roles open in the site.

Closed by `sh finance/scripts/swiss-setup.sh keyaccess` (re-runnable; it says
"nothing to change" on a second run, reads back at the end and stops on a
mismatch, printing role and report names only):

- Employee: the level 0 rows that give a key role `select` and no `read`
  (Accounts User, Sales Master Manager) are removed. Frappe lets a select-only
  user list a doctype and read its search field, here the employee's name. A key
  role with `read` on Employee stops the step. Accounts Manager and the HR roles
  keep their rows.
- Leave Ledger Entry: the row of role All (permlevel 0, only if creator) is
  removed. It carried create, write, submit and delete as well, and nothing in
  hrms goes through it: ledger rows are written with `ignore_permissions` and
  read by query. System Manager, HR Manager and HR User stay.
- The reports Cash Flow Forecast (net pay per run), Cash Flow Forecast Lines
  (the same computation, line by line), Project Profitability (gross pay per
  slip), Monthly Worktime and Worktime Overview (given an employee id they read
  name, target and actual hours and leave balance past the Employee permission,
  with no Timesheet row needed) get a Custom Role without the key roles. A
  report with no roles is open to everyone, so an empty result stops the step.
- Payment Proposal: the `salaries` and `expenses` tables, and the employee, slip,
  claim, amount, account and date fields of their two child doctypes, are raised
  to permlevel 1, with level 1 read and write for Accounts Manager and System
  Manager added beside the level 0 rows (Accounts User keeps those). A key then
  gets neither the tables from a document read or a list, nor the fields from
  the child doctypes listed directly. The step stops if a key role holds level 1
  on the doctype, if Accounts Manager or System Manager has no level 0 row on it
  (Frappe refuses a level 1 row for a role without one), or if an enabled person
  holds Accounts User without Accounts Manager or System Manager (that person
  would lose the tables). The child-field closure holds only while no key role
  has a level 1 row on Payment Proposal (a direct child list is judged by the
  parent's permissions); the check has a "level1" line for that.

What `finance/bexio/api_key_check.py` asks of a key: one row from every doctype of
the hrms and BI Payroll modules, from Employee, Deleted Document and Version, from
File on Salary Slip and on Employee, for the five reports above whether
`frappe.desk.query_report.get_script` answers, and employee and amount from
Payment Proposal Salary Slip and Payment Proposal Expense listed directly with
parent=Payment Proposal. The report call passes the same gate as the report and
returns no report data, so the check never runs a report. It also asks two
whitelisted methods the method gate below must refuse, each for one row:
`erpnext.controllers.queries.employee_query` and `frappe.desk.search.search_link`
with `query` set to it; the lines start `method`, and an answer is dropped on the
spot. A method that writes is never probed. Exit 0 means every line was refused
(the File and child lines may be empty: File is filtered row by row, and without
a proposal there is no child row to show).

Sweep of whitelisted methods (erp-qg70, read from source at the image's pins): every
`@frappe.whitelist` function of hrms (221) and bi_payroll (4), and in erpnext and
erpnextswiss the ones in a file that names Salary Slip, net_pay or gross_pay, and
those whose body names Employee or Payroll. Document methods are guarded by Frappe's
read check on the document; functions that answer for the session user only, or
check permission first, are fine.

The sweep found nine functions that answer a caller holding only the key roles,
among them `create_payment_proposal_for_mcp` in erpnextswiss (salary and expense
lines) and `erpnext.controllers.queries.employee_query` (employee names); the full
list is in the notes of erp-qg70. Fixing them one by one would leave whatever the sweep
did not read, and `frappe.desk.search.search_link` calls any whitelisted query by name,
so a ban on single names is not enough. The method gate (erp-kb06) turns it round:

- Who: `api-sync@finance.local` and `api-mcp@finance.local` (`GATED_USERS` in
  `bi_finance/api_gate.py`). `api-agent`, humans and Guest pass unchanged; the
  importers stay on `api-agent` until erp-tamd.
- What: a request that calls a whitelisted method, by `/api/method/<m>`,
  `/api/v1/method/<m>`, `/api/v2/method/<m>`, `/api/v2/method/<doctype>/<m>`, or by
  `cmd` in the query or the body on any path, must name a method in `ALLOWED_METHODS`
  of the same file, an exact dotted name with no prefix match. Anything else answers
  403 and the message names the method only. The list is short: `ping`,
  `frappe.ping`, `frappe.auth.get_logged_user`,
  `frappe.desk.form.load.getdoctype` (the sync reads doctype meta),
  `frappe.desk.query_report.run` (the ledger reports an MCP session asks), and
  `frappe.desk.query_report.get_script`. `frappe.desk.search` and
  `run_doc_method` are not on it; neither are methods that write or methods in
  hrms, erpnextswiss or bi_finance.
- What it leaves alone: `/api/resource/...` and `/api/v2/document/...`, including
  document methods. Frappe checks the document's permission there, and the key roles
  and `keyaccess` are what limit them.
- Where it hooks: `auth_hooks` in `bi_finance/hooks.py`, not `before_request`. Frappe
  validates a token after the `before_request` hooks have run, so there the user is
  still Guest; `auth_hooks` run at the end of the authentication, with the key's user
  set.
- Add a method: put its exact name into `ALLOWED_METHODS`, read the function to the
  end first (it must read or write nothing past the caller's own permissions, and it
  must not be able to reach payroll, HR or Employee data), and ship it with the next
  image. Until then it answers 403 to both keys; the MCP session reports "Permission
  denied (403)". There is no setting that changes the list without an image.

The salary and expense tables of Payment Proposal, which Accounts User reads, are
closed by keyaccess (erp-btgr), not by the gate: they are a document, not a method.

What the proof does not cover:

- The Payment Proposal tables above, until keyaccess has run on live (erp-95bt). Live
  holds no proposal yet, so the child-doctype lines of the check come back empty and
  prove nothing until the first one exists (erp-nwbd reads it with the MCP key). The
  gate refuses the nine functions for the two keys; `api-agent` is not gated, so its
  key still reaches them (erp-tamd, erp-c05w).
- bi_finance's own whitelisted functions were not swept; the method gate refuses them
  for the two keys and none is on its list.
- Paths that are no method call and no document: file downloads under `/private/files`
  (File on Salary Slip and on Employee are asked by the check), website pages, and
  `/api/v2/doctype/<doctype>/meta` (schema only, no rows).
- Salary traces in the ledger (GL Entry, Journal Entry, Payment Entry, Bank
  Transaction): Benchi decides through yard-xsab.
- Eight other reports the key roles open name employees only through Timesheet or
  party rows: Accounts Receivable, Daily Timesheet Summary, General Ledger, Service
  Invoicing, Timesheet Billing Summary, Timesheet Month per Activity, Timesheet
  Month per Project and Trial Balance for Party. Live has no Timesheet and no
  Employee party posting; if that changes, they are read again.
- Dashboards, number cards and other generic frappe endpoints were not swept.
  The check shows what it asks, no more.

## Not covered

- Write tools (`create_doc`, `update_doc`, `delete_doc`, `run_method` with
  side effects) are not tested against real data. The brief leaves that out.
- Registering the server in the global Claude config: the yardmaster does
  that after merge.
- Upgrading the pin: change `requirements.txt`, re-run the install, and re-run
  the read test.
