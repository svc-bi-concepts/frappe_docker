#!/bin/sh
# Provision the ERPNext users that scripts and agents call the API as, so
# nothing uses Administrator. Re-runnable: a second run changes nothing, and
# keys are rotated only with --rotate (or when the keys file is missing,
# because the secret cannot be read back from the site).
#
#   --user agent  api-agent@finance.local, the importers (default), keys in .erpnext-api
#   --user sync   api-sync@finance.local, the hourly sync, keys in .erpnext-api-sync
#   --user mcp    api-mcp@finance.local, the MCP sessions, keys in .erpnext-api-mcp
#
# Writes url=, api_key= and api_secret= to ~/ws_yardr_finance/<keys file>
# (mode 600). Neither key nor secret is printed.
#
# agent only ever adds roles (System Manager stays: erp-wnx7). sync and mcp hold
# exactly their list: a run removes every other role, and the script refuses to
# run when either list holds a payroll or approval role or System Manager, so a
# later edit cannot widen them unnoticed.
set -eu

ROTATE=0
USER_KIND=agent
while [ $# -gt 0 ]; do
  case "$1" in
  --rotate) ROTATE=1 ;;
  --user)
    [ $# -ge 2 ] || {
      echo "usage: $0 [--user agent|sync|mcp] [--rotate]" >&2
      exit 2
    }
    USER_KIND=$2
    shift
    ;;
  *)
    echo "usage: $0 [--user agent|sync|mcp] [--rotate]" >&2
    exit 2
    ;;
  esac
  shift
done

# The master managers and Item Manager are what the bexio master-data import
# needs to create Supplier, Customer/Supplier Group, Item and Item Price. No
# System Manager: Bank, Currency and Designation stay out of its reach.
# HR User is for the employee import (import_employees.py): it may write
# Employee and read the HR master data (Gender, Employment Type).
AGENT_ROLES="Accounts User|Sales User|Purchase User|Stock User|Sales Master Manager|Purchase Master Manager|Item Manager|HR User"
# The hourly sync writes draft invoices only: the agent list without HR User.
SYNC_ROLES="Accounts User|Sales User|Purchase User|Stock User|Sales Master Manager|Purchase Master Manager|Item Manager"
# The MCP session reads and drafts; it holds no payroll role (HR User reads Employee
# and Salary Slip) and no System Manager, and what it reads goes to the model provider.
MCP_ROLES="Accounts User|Sales User|Purchase User|Stock User"
# Roles no sync or mcp list may ever hold.
FORBIDDEN_ROLES="System Manager|HR User|HR Manager|AI Approver"

OLD_IFS=$IFS
IFS='|'
for role in $FORBIDDEN_ROLES; do
  for list in "$SYNC_ROLES" "$MCP_ROLES"; do
    if printf '%s\n' "$list" | tr '|' '\n' | grep -qxF "$role"; then
      echo "create-api-user: refusing to run: the sync or mcp role list holds $role" >&2
      exit 2
    fi
  done
done
IFS=$OLD_IFS

case "$USER_KIND" in
agent)
  EMAIL=api-agent@finance.local
  FIRST_NAME="API Agent"
  ROLES=$AGENT_ROLES
  EXACT=0
  KEYS_NAME=.erpnext-api
  ;;
sync)
  EMAIL=api-sync@finance.local
  FIRST_NAME="API Sync"
  ROLES=$SYNC_ROLES
  EXACT=1
  KEYS_NAME=.erpnext-api-sync
  ;;
mcp)
  EMAIL=api-mcp@finance.local
  FIRST_NAME="API MCP"
  ROLES=$MCP_ROLES
  EXACT=1
  KEYS_NAME=.erpnext-api-mcp
  ;;
*)
  echo "usage: $0 [--user agent|sync|mcp] [--rotate]" >&2
  exit 2
  ;;
esac

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

PROJECT=frappe-finance
# Port 8080 is IT's front door: it forwards to the stack's 8082 (Colima is always on, nothing to wake).
API_URL=http://127.0.0.1:8080/api/
OUT="$HOME/ws_yardr_finance/$KEYS_NAME"

FORCE=0
if [ "$ROTATE" = 1 ] || [ ! -f "$OUT" ]; then
  FORCE=1
fi

# Runs inside the backend container with bench's own Python, so frappe loads
# the site without an interactive console. Prints one marked JSON line; the
# keys appear in it only when they were generated.
CONTAINER_PY=$(
  cat <<'PY'
import json, sys
import frappe
from frappe.core.doctype.user.user import generate_keys

force = sys.argv[1] == "1"
EMAIL, FIRST_NAME = sys.argv[2], sys.argv[3]
ROLES = sys.argv[4].split("|")
exact = sys.argv[5] == "1"
# Frappe adds these itself; an exact list leaves them alone.
AUTOMATIC = {"All", "Desk User"}

frappe.init(site="frontend", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

result = {"created": False, "roles_added": [], "roles_removed": [], "roles": [], "keys": None}
if not frappe.db.exists("User", EMAIL):
    user = frappe.get_doc({
        "doctype": "User",
        "email": EMAIL,
        "first_name": FIRST_NAME,
        "user_type": "System User",
        "enabled": 1,
        "send_welcome_email": 0,
        "roles": [{"role": role} for role in ROLES],
    })
    user.insert(ignore_permissions=True)
    result["created"] = True
    result["roles_added"] = ROLES
else:
    user = frappe.get_doc("User", EMAIL)
    have = {row.role for row in user.roles}
    missing = [role for role in ROLES if role not in have]
    for role in missing:
        user.append("roles", {"role": role})
    extra = sorted(have - set(ROLES) - AUTOMATIC) if exact else []
    if extra:
        user.set("roles", [row for row in user.roles if row.role not in extra])
    if missing or extra:
        user.save(ignore_permissions=True)
        result["roles_added"] = missing
        result["roles_removed"] = extra

result["roles"] = sorted(row.role for row in user.roles)
if force or not user.api_key:
    result["keys"] = generate_keys(EMAIL)
frappe.db.commit()
print("FINANCE_API_USER " + json.dumps(result))
PY
)

RESULT=$(printf '%s\n' "$CONTAINER_PY" | docker compose -p "$PROJECT" -f pwd.yml -f finance-local.yml \
  exec -T -w /home/frappe/frappe-bench/sites backend \
  /home/frappe/frappe-bench/env/bin/python -I - "$FORCE" "$EMAIL" "$FIRST_NAME" "$ROLES" "$EXACT") || {
  echo "create-api-user: the backend container did not run the provisioning step" >&2
  exit 1
}

# Host side: write the keys file with mode 600 from a temp file and rename it
# into place, so a failed run never leaves a half-written file behind.
HOST_PY=$(
  cat <<'PY'
import json, os, sys
out, api_url, email, exact = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
line = next((l for l in sys.stdin if l.startswith("FINANCE_API_USER ")), None)
if line is None:
    sys.exit("create-api-user: no result line from the site")
res = json.loads(line[len("FINANCE_API_USER "):])
if res["created"]:
    print("created user " + email + " (no login password, System User)")
if res["roles_added"]:
    print("roles set: " + ", ".join(res["roles_added"]))
if res["roles_removed"]:
    print("roles removed: " + ", ".join(res["roles_removed"]))
if exact:
    print("roles now: " + ", ".join(res["roles"]))
keys = res["keys"]
if keys:
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("url=%s\napi_key=%s\napi_secret=%s\n" % (api_url, keys["api_key"], keys["api_secret"]))
    os.replace(tmp, out)
    print("wrote " + out + " (mode 600); key and secret not shown")
else:
    print("keys unchanged (" + out + ")")
PY
)
printf '%s\n' "$RESULT" | python3 -c "$HOST_PY" "$OUT" "$API_URL" "$EMAIL" "$EXACT"
