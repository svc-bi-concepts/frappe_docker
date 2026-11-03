#!/bin/sh
# Start the ERPNext MCP server as the MCP API user (api-mcp@finance.local, no
# payroll role). Claude Code runs this as its MCP command (see mcp.example.json).
# The credentials are read from ~/ws_yardr_finance/.erpnext-api-mcp at each
# start and passed on as environment, so neither the key nor the secret is
# written to the repo or to argv.
# Until keys split 2 has made that file, it falls back to .erpnext-api (the
# agent user, which reads payroll) and says so on stderr; the fallback goes
# once the split has passed.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
KEYS="$HOME/ws_yardr_finance/.erpnext-api-mcp"

if [ ! -f "$KEYS" ]; then
  KEYS="$HOME/ws_yardr_finance/.erpnext-api"
  if [ ! -f "$KEYS" ]; then
    echo "run-frappe-mcp: no key file; run finance/scripts/create-api-user.sh --user mcp" >&2
    exit 1
  fi
  echo "run-frappe-mcp: warning: $HOME/ws_yardr_finance/.erpnext-api-mcp is missing, using the agent key $KEYS (it reads payroll); run finance/scripts/create-api-user.sh --user mcp" >&2
fi

# The keys file holds the API URL with a trailing /api/; the client adds
# /api/ to its base URL itself, so strip it here.
FRAPPE_URL=
FRAPPE_API_KEY=
FRAPPE_API_SECRET=
while IFS='=' read -r key value; do
  case "$key" in
  url) FRAPPE_URL="$value" ;;
  api_key) FRAPPE_API_KEY="$value" ;;
  api_secret) FRAPPE_API_SECRET="$value" ;;
  esac
done <"$KEYS"
FRAPPE_URL="${FRAPPE_URL%/}"
FRAPPE_URL="${FRAPPE_URL%/api}"
export FRAPPE_URL FRAPPE_API_KEY FRAPPE_API_SECRET

exec "$HERE/venv/bin/frappe-mcp-server"
