#!/bin/sh
# Proves from outside that the ledger is not reachable from the public side: run it from a machine outside the
# VM's private network, after finance-coolify.yml is up. nginx in the image serves the ledger for ANY Host header
# (pwd.yml: FRAPPE_SITE_NAME_HEADER: frontend), so the router's one Host rule is the only barrier; this asks the
# public addresses the questions a stranger would.
#
# usage: finance/scripts/public-check.sh
#   Reads private/public-check-hosts (mode 600, never in the repo; PUBLIC_CHECK_HOSTS names another file): one line per
#   public address, "<address> [<public name> ...]", a line starting with # is a comment. Each address is asked, on HTTP and HTTPS,
#   with each listed public name, an unknown name, an empty Host and the bare address; HTTPS sets the SNI to match
#   the name (--resolve), the empty Host and the bare address send none. Each is asked for /api/method/ping and /login.
#   Exit 0: no answer was Frappe's (no "pong", no login page, no Frappe header); a host that refuses every
#   connection passes. Exit 1: some case answered as Frappe; the output names the case only, never the host.
#   Exit 2: the file is missing, has the wrong mode or no address, or an address does not resolve.
set -eu

case "${1:-}" in
-h | --help)
  sed -n '/^# usage/,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
  ;;
esac

# The private folder sits at the repository root, two levels up from this script.
FILE=${PUBLIC_CHECK_HOSTS:-"$(dirname "$0")/../../private/public-check-hosts"}

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
  case $rc in
  0) ;;
  6)
    echo "public-check: $label: the address does not resolve" >&2
    exit 2
    ;;
  *)
    refused=$((refused + 1))
    return 0
    ;;
  esac
  answered=$((answered + 1))
  if printf '%s' "$out" | grep -Eiq "$FRAPPE"; then
    hits=$((hits + 1))
    echo "HIT: $label, $scheme, $path"
  fi
}

# probe <kind> <label> <scheme> <name> <addr>: one way in, for both paths. kind is named (a listed or the unknown
# name, with the SNI set to it), empty (no Host header) or bare (the address itself as the host).
probe() {
  kind=$1 label=$2 scheme=$3 name=$4 addr=$5
  port=80
  [ "$scheme" = https ] && port=443
  for path in /api/method/ping /login; do
    case $kind in
    empty) ask "$label" "$scheme" "$path" -H 'Host:' "$scheme://$addr$path" ;;
    bare) ask "$label" "$scheme" "$path" "$scheme://$addr$path" ;;
    *) ask "$label" "$scheme" "$path" --resolve "$name:$port:$addr" "$scheme://$name$path" ;;
    esac
  done
}

while read -r addr names; do
  case $addr in
  '' | '#'*) continue ;;
  esac
  addr_no=$((addr_no + 1))
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
