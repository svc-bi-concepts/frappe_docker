#!/bin/sh
# Prints the image tag the in-image step of .yardr/check runs. A branch cut before the last
# image switch pins a tag the mini has removed (the previous image goes after each switch), so
# the step takes main's deploy pin first (present after every switch), then the branch's own
# pin, then the one frappe-finance-custom image at rest. Nothing is pulled from a registry.
#
# usage: finance/scripts/gate-image.sh
#   Prints the image reference (repository:tag) on stdout. When the branch pins another tag than main, one warning line on
#   stderr says to rebase onto main. Exits 1 when none of those tags is present, naming them.
set -eu

case "${1:-}" in
  -h|--help) echo "usage: finance/scripts/gate-image.sh"; exit 0 ;;
esac

REPO=frappe-finance-custom

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# The tag each compose file names, read the way switch-image.sh reads it. Empty when unreadable.
BRANCH=$(python3 finance/scripts/switch_image.py current finance-local.yml)
MAIN=$(git show origin/main:finance-local.yml 2>/dev/null | python3 finance/scripts/switch_image.py current /dev/stdin 2>/dev/null) || MAIN=""

present() {
  docker image inspect "$REPO:$1" >/dev/null 2>&1
}

if [ -n "$MAIN" ] && present "$MAIN"; then
  TAG=$MAIN
elif present "$BRANCH"; then
  TAG=$BRANCH
else
  REST=$(docker images --format '{{.Tag}}' "$REPO")
  # Word splitting drops the blank lines of an empty list.
  set -- $REST
  if [ $# -ne 1 ]; then
    echo "gate-image: no $REPO image to gate with: looked for origin/main's ${MAIN:-(unreadable)} and the branch's $BRANCH, and $# images are at rest" >&2
    exit 1
  fi
  TAG=$1
fi

if [ "$BRANCH" != "$MAIN" ] || [ "$TAG" != "$MAIN" ]; then
  echo "gate-image: origin/main pins $REPO:${MAIN:-(unreadable)}, this branch pins $REPO:$BRANCH, the gate runs $REPO:$TAG: rebase onto main" >&2
fi

echo "$REPO:$TAG"
