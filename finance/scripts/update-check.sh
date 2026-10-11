#!/bin/sh
# D4, the monthly check (finance/docs/operating.md, "Monthly update (D4)"): which pinned ERPNext
# apps have a newer patch release of their minor. Read only: no docker, nothing written; the
# upstream tags come from git ls-remote. Exit 0 always; an unreachable upstream prints "unknown".
#
# usage: finance/scripts/update-check.sh [--minor]
set -eu

# The pins and the upstream repos are read from the repository root, two levels up from here.
cd "$(dirname "$0")/../.."
exec python3 finance/scripts/update_check.py "$@"
