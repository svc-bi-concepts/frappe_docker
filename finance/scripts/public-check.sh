#!/bin/sh
# Proves from outside that the ledger is not reachable from the public side: run it from a machine outside the
# VM's private network, after finance-coolify.yml is up. nginx in the image serves the ledger for ANY Host header
# (pwd.yml: FRAPPE_SITE_NAME_HEADER: frontend), so the router's one Host rule is the only barrier; this asks the
# public addresses the questions a stranger would.
#
# usage: finance/scripts/public-check.sh
#   Reads public-check-hosts (mode 600) in $FINANCE_PRIVATE (default: the private/ folder beside the finance checkout,
#   never in the repo; PUBLIC_CHECK_HOSTS names another file): one line per public address, "<IPv4 or IPv6 address>
#   [<public name> ...]", a line starting with # is a comment. Each address is asked, on HTTP and HTTPS,
#   with each listed public name, an unknown name, an empty Host and the bare address; HTTPS sets the SNI to match
#   the name (--resolve), the empty Host and the bare address send none. Each is asked for /api/method/ping and /login.
#   Exit 0: no answer was Frappe's (no "pong", no login page, no Frappe header); a host that refuses every
#   connection passes. Exit 1: some case answered as Frappe; the output names the case only, never the host.
#   Exit 2: the check could not ask: the file is missing, has the wrong mode or no address, an address is no IP
#   address, or curl is missing or failed in any way that is not the host turning the connection down.
set -eu

case "${1:-}" in
-h | --help)
  sed -n '/^# usage/,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
  ;;
esac

# private/ sits beside the finance checkout and is never linked into a worktree, so it is found from the checkout's .git.
FILE=${PUBLIC_CHECK_HOSTS:-${FINANCE_PRIVATE:-$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)/../../private}/public-check-hosts}

if [ ! -f "$FILE" ]; then
  echo "public-check: no hosts file (private/public-check-hosts)" >&2
  exit 2
fi
# stat -f is BSD/macOS, stat -c is GNU.
MODE=$(stat -f %Lp "$FILE" 2>/dev/null || stat -c %a "$FILE")
if [ "$MODE" != 600 ]; then
  echo "public-check: the hosts file must be mode 600, it is $MODE" >&2
  exit 2
fi

# Frappe's answer in any form: the ping's pong, the login page (it loads frappe), or a Frappe header.
FRAPPE='pong|frappe|erpnext'

hits=0
refused=0
answered=0
asked=0
addr_no=0

# ask <label> <scheme> <path> <curl args...>: one request. A connection that fails, refuses or resets is no answer.
ask() {
  label=$1 scheme=$2 path=$3
  shift 3
  asked=$((asked + 1))
  rc=0
  out=$(curl -sS -k -i -m 10 "$@" 2>/dev/null) || rc=$?
  # Read before the exit code: an answer cut short (a timeout, a reset) can already show Frappe.
  if printf '%s' "$out" | grep -Eiq "$FRAPPE"; then
    hits=$((hits + 1))
    echo "HIT: $label, $scheme, $path"
  fi
  case $rc in
  0) answered=$((answered + 1)) ;;
  # The host turned it down: refused, timed out, TLS refused, empty reply, reset. Any other code is curl
  # failing to ask (127 no curl, 3 a bad URL, 49 a bad --resolve), and a question not asked proves nothing.
  7 | 28 | 35 | 52 | 56) refused=$((refused + 1)) ;;
  *)
    echo "public-check: $label: curl failed with exit code $rc, the check proves nothing" >&2
    exit 2
    ;;
  esac
}

# probe <kind> <label> <scheme> <name> <addr>: one way in, for both paths. kind is named (a listed or the unknown
# name, with the SNI set to it), empty (a Host header with no value: curl's 'Host:' would drop the header instead)
# or bare (the address itself as the host).
probe() {
  kind=$1 label=$2 scheme=$3 name=$4 addr=$5
  port=80
  [ "$scheme" = https ] && port=443
  # A URL and --resolve both take an IPv6 address in brackets.
  case $addr in
  *:*) host="[$addr]" ;;
  *) host=$addr ;;
  esac
  for path in /api/method/ping /login; do
    case $kind in
    empty) ask "$label" "$scheme" "$path" -H 'Host;' "$scheme://$host$path" ;;
    bare) ask "$label" "$scheme" "$path" "$scheme://$host$path" ;;
    *) ask "$label" "$scheme" "$path" --resolve "$name:$port:$host" "$scheme://$name$path" ;;
    esac
  done
}

# The || keeps a last line with no newline: read reports it as the end of the file.
while read -r addr names || [ -n "$addr" ]; do
  case $addr in
  '' | '#'*) continue ;;
  esac
  addr_no=$((addr_no + 1))
  if ! python3 -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$addr" 2>/dev/null; then
    echo "public-check: address $addr_no is not an IP address" >&2
    exit 2
  fi
  for scheme in http https; do
    n=0
    for name in $names; do
      n=$((n + 1))
      probe named "address $addr_no, public name $n" "$scheme" "$name" "$addr"
    done
    probe named "address $addr_no, unknown name" "$scheme" unknown-name.invalid "$addr"
    probe empty "address $addr_no, empty Host" "$scheme" "" "$addr"
    probe bare "address $addr_no, bare address" "$scheme" "" "$addr"
  done
done <"$FILE"

if [ "$addr_no" -eq 0 ]; then
  echo "public-check: the hosts file names no address" >&2
  exit 2
fi

echo "public-check: $addr_no address(es), $asked requests, $refused refused, $answered answered, $hits Frappe"
[ "$hits" -eq 0 ]
