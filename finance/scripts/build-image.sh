#!/bin/sh
# Build a finance image from one of two app lists, as a base layer (ERPNext and the
# apps) plus the bi_finance layer and the bi_payroll layer. Builds only; it does not
# touch the running stack. Switching a stack to the image is a later step, after a backup.
#
# usage: finance/scripts/build-image.sh live|copy <tag>
#   live  finance/apps.json: ERPNext, erpnextswiss and hrms (payroll runs on the live
#         site, finance/docs/hrms.md, Decision 2026-10-10).
#   copy  finance/apps-copy.json: the same list; kept as the HRMS copy site's own list
#         (finance/docs/hrms.md).
# result: frappe-finance-custom:<tag>, refused if that tag (or its -base or -finance
#         layer) already exists, so a build can never overwrite a tag in use.
#
# Builds run on the capped buildx builder only (BUILDER, default capped): a plain
# 'docker build' on the default builder shares the colima VM's memory with the live
# stack and OOM-killed the live containers twice on 2026-10-11. The app list goes in
# as a secret, not a build arg, so nothing from it ends up in image metadata.
#
# NODE_HEAP_MB caps the V8 heap of the asset build (default 900). The cap bounds the heap
# only, so esbuild and the rest of the node process need room under the builder's cap too.
# It only counted after finance/patches/frappe: frappe's bundler used to replace the
# NODE_OPTIONS it was given with 75% of the RAM it sees (about 2.9G in the builder VM), and
# a bi14 build died with status 137 (SIGKILL) at the hrms vite step under 900 and under 1536.
#
# The capped builder (docker-container driver) cannot see the local image store, so the
# bi_finance and bi_payroll layers cannot FROM the layer before by its tag: each build
# also writes its image as an OCI layout in a scratch directory, and the next build gets
# that as the named context for its BASE.
set -eu

usage() {
  echo "usage: finance/scripts/build-image.sh live|copy <tag>"
  echo "  live  finance/apps.json (with hrms)"
  echo "  copy  finance/apps-copy.json (with hrms)"
}

case "${1:-}" in
-h | --help)
  usage
  exit 0
  ;;
live) APPS=finance/apps.json ;;
copy) APPS=finance/apps-copy.json ;;
*)
  usage >&2
  exit 2
  ;;
esac
if [ $# -ne 2 ]; then
  usage >&2
  exit 2
fi
MODE="$1"
TAG="$2"
FRAPPE_TAG=v16.51.0

# The asset build's V8 heap cap in MB (see the header). A number only: it goes into a flag.
NODE_HEAP_MB="${NODE_HEAP_MB:-900}"
case "$NODE_HEAP_MB" in
'' | *[!0-9]*)
  echo "refused: NODE_HEAP_MB must be a number of MB, not '$NODE_HEAP_MB'" >&2
  exit 2
  ;;
esac

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# The capped builder must exist: builds never run on the default builder (see the
# header). BUILDER names another builder, for a machine that named its own.
BUILDER="${BUILDER:-capped}"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "refused: buildx builder '$BUILDER' does not exist; ask IT for the capped builder (finance/docs/erpnext-setup.md, Building an image)" >&2
  exit 1
fi

# The copy image is built from a list with hrms; a list without it is a mistake.
python3 - "$APPS" "$MODE" <<'PY'
import json, sys

apps_path, mode = sys.argv[1], sys.argv[2]
names = [app["url"].rstrip("/").rsplit("/", 1)[-1] for app in json.load(open(apps_path))]
if mode == "copy" and "hrms" not in names:
    sys.exit(f"refused: {apps_path} has no hrms; the copy image is built from it")
PY

# Refuse to overwrite a tag that exists: a live tag stays what it was built as.
if docker image inspect "frappe-finance-custom:$TAG" >/dev/null 2>&1 ||
  docker image inspect "frappe-finance-custom:$TAG-base" >/dev/null 2>&1 ||
  docker image inspect "frappe-finance-custom:$TAG-finance" >/dev/null 2>&1; then
  echo "refused: frappe-finance-custom:$TAG (or its -base or -finance) already exists; pick a new tag" >&2
  exit 1
fi

# bench clones each app with `git clone --branch`, which takes a branch or tag,
# not a commit. So apps.json pins erpnextswiss to a branch and records the
# commit in "commit". Refuse to build if the branch has moved off that commit.
# The check runs before the build, so a move after it is not caught.
python3 - "$APPS" <<'PY'
import json, subprocess, sys

for app in json.load(open(sys.argv[1])):
    pinned = app.get("commit")
    if not pinned:
        continue
    out = subprocess.run(
        ["git", "ls-remote", app["url"], "refs/heads/" + app["branch"]],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    head = out[0] if out else ""
    if head != pinned:
        sys.exit(f"{app['url']} {app['branch']} is at {head or 'nothing'}, the list pins {pinned}")
    print(f"pinned: {app['url']} {app['branch']} @ {pinned}")
PY

# OCI layouts of the base and finance layers, for the next build's BASE (see the header).
OCI="$(mktemp -d)"
trap 'rm -rf "$OCI"' EXIT

# --no-cache: a secret is not part of the layer cache key, so a cached bench init
# layer would keep the apps from an earlier apps list.
docker buildx build \
  --builder "$BUILDER" \
  --load \
  --output "type=oci,tar=false,dest=$OCI/base" \
  --no-cache \
  --build-arg FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg FRAPPE_BRANCH="$FRAPPE_TAG" \
  --build-arg NODE_OPTIONS=--max-old-space-size="$NODE_HEAP_MB" \
  --secret id=apps_json,src="$APPS" \
  --tag "frappe-finance-custom:$TAG-base" \
  --file images/custom/Containerfile .

# bi_finance and bi_payroll are this repo's own apps, not in the app lists: a layer each
# on the base image. bi_payroll requires hrms, which the base carries.
docker buildx build \
  --builder "$BUILDER" \
  --load \
  --output "type=oci,tar=false,dest=$OCI/finance" \
  --build-context "frappe-finance-custom:$TAG-base=oci-layout://$OCI/base:$TAG-base" \
  --build-arg BASE="frappe-finance-custom:$TAG-base" \
  --tag "frappe-finance-custom:$TAG-finance" \
  --file finance/images/bi_finance.Containerfile .

docker buildx build \
  --builder "$BUILDER" \
  --load \
  --build-context "frappe-finance-custom:$TAG-finance=oci-layout://$OCI/finance:$TAG-finance" \
  --build-arg BASE="frappe-finance-custom:$TAG-finance" \
  --tag "frappe-finance-custom:$TAG" \
  --file finance/images/bi_payroll.Containerfile .

echo "built frappe-finance-custom:$TAG from $APPS"
