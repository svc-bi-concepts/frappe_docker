#!/bin/sh
# The copy stack for the HR train: a copy of the live books on a train image, started on demand, so the
# children can prove their work on it (finance/docs/hrms.md, 'Copy stack for the HR train'). The live
# stack is never started, stopped or changed from here.
#
# usage: finance/scripts/copy-stack.sh up <image-tag>   start the copy and restore a fresh live backup into it
#        finance/scripts/copy-stack.sh refresh          restore a fresh live backup into the running copy
#        finance/scripts/copy-stack.sh down             stop the copy (keeps its volumes)
#        finance/scripts/copy-stack.sh remove           stop the copy and delete its volumes and staged backups
#   The image frappe-finance-custom:<image-tag> must exist (build-image.sh copy builds it).
#   The copy's frontend listens on 127.0.0.1:8815. Nothing is served on the tailnet here: the yardmaster
#   does that with tailscale serve when Benchi needs to look.
#
# Where the staged backups and the copy's Administrator password live: $FINANCE_PRIVATE (default: the
# private/ folder of the finance checkout, which is never linked into a worktree).
set -eu

usage() {
  echo "usage: finance/scripts/copy-stack.sh up <image-tag> | refresh | down | remove"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

CMD="${1:-}"
case "$CMD" in
  up)
    if [ $# -ne 2 ]; then usage >&2; exit 2; fi
    TAG="$2"
    ;;
  refresh|down|remove)
    if [ $# -ne 1 ]; then usage >&2; exit 2; fi
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

# The copy's compose project. Only frappe-finance-copy* is accepted: the live project (frappe-finance) is refused.
PROJECT="${COPY_PROJECT:-frappe-finance-copy}"
case "$PROJECT" in
  frappe-finance-copy*) ;;
  *)
    echo "refused: project $PROJECT is not the copy's (frappe-finance-copy); this script never runs the live stack" >&2
    exit 1
    ;;
esac

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

PRIVATE="${FINANCE_PRIVATE:-/Users/bsaladin/ws_yardr_finance/private}"
STAGE="$PRIVATE/copy-restore"
PW_FILE="$PRIVATE/.erpnext-admin-copy"
LIVE_BACKEND="frappe-finance-backend-1"
LIVE_BACKUPS="/home/frappe/frappe-bench/sites/frontend/private/backups"
# MemAvailable the copy needs before it starts (2.5 GiB, in kB as /proc/meminfo prints it).
MEM_NEEDED_KB=2621440

copy_compose() {
  docker compose -p "$PROJECT" -f pwd.yml -f finance-copy.yml "$@"
}

refuse() {
  echo "refused: $1" >&2
  exit 1
}

require_image() {
  if ! docker image inspect "frappe-finance-custom:$1" >/dev/null 2>&1; then
    refuse "frappe-finance-custom:$1 is not built; run finance/scripts/build-image.sh copy <tag> first"
  fi
}

check_memory() {
  kb=$(docker run --rm --entrypoint cat "frappe-finance-custom:$1" /proc/meminfo | awk '/^MemAvailable:/ {print $2}')
  case "$kb" in
    ''|*[!0-9]*) refuse "could not read the free memory of the VM" ;;
  esac
  if [ "$kb" -lt "$MEM_NEEDED_KB" ]; then
    refuse "free memory is $((kb / 1024)) MiB; the copy needs 2560 MiB. Stop something first"
  fi
}

copy_running() {
  if [ -z "$(copy_compose ps -q backend)" ]; then
    refuse "the copy is not running; start it with: copy-stack.sh up <image-tag>"
  fi
}

# A fresh backup of the live site (it only reads the books), and its four files staged in $STAGE.
# Sets PREFIX, the timestamp prefix of the four files.
live_backup() {
  docker exec "$LIVE_BACKEND" bench --site frontend backup --with-files >/dev/null
  DB=$(docker exec "$LIVE_BACKEND" sh -c "ls -t $LIVE_BACKUPS/*-database.sql.gz | head -1")
  PREFIX=$(basename "${DB%-database.sql.gz}")
  case "$PREFIX" in
    ''|*' '*) refuse "no backup of the live site was found" ;;
  esac
  mkdir -p "$STAGE"
  chmod 700 "$STAGE"
  for suffix in database.sql.gz site_config_backup.json files.tar private-files.tar; do
    docker cp "$LIVE_BACKEND:$LIVE_BACKUPS/$PREFIX-$suffix" "$STAGE/"
  done
}

# Copies the staged backup into the copy's backups folder and restores it over the copy's site.
restore_copy() {
  COPY_BACKUPS="/home/frappe/frappe-bench/sites/frontend/private/backups"
  for suffix in database.sql.gz site_config_backup.json files.tar private-files.tar; do
    copy_compose cp "$STAGE/$PREFIX-$suffix" "backend:$COPY_BACKUPS/"
  done
  copy_compose exec -T backend bench --site frontend restore \
    "$COPY_BACKUPS/$PREFIX-database.sql.gz" \
    --with-public-files "$COPY_BACKUPS/$PREFIX-files.tar" \
    --with-private-files "$COPY_BACKUPS/$PREFIX-private-files.tar" \
    --db-root-password admin --force
}

# Settings that keep the copy from acting like live. The restore brings the live site config and database
# back, so these run after every restore. The site-config ones first, so no scheduled job runs during migrate.
safety() {
  copy_compose exec -T backend bench --site frontend set-config -p pause_scheduler 1
  copy_compose exec -T backend bench --site frontend set-config -p mute_emails 1
  # The copy's nginx is in-network, so wkhtmltopdf finds assets; live's host_name comes from HOST_NAME in swiss-setup.sh and must never be copied here.
  copy_compose exec -T backend bench --site frontend set-config host_name http://frontend:8080
  # The train image may carry patches the live site has not run.
  copy_compose exec -T backend bench --site frontend migrate
  copy_compose exec -T backend bench --site frontend set-maintenance-mode off
  # The bank API syncs (hourly on live) and outgoing mail, off on the copy.
  copy_compose exec -T backend bench --site frontend execute frappe.db.set_single_value --args '["Wise Settings", "sync_enabled", 0]'
  copy_compose exec -T backend bench --site frontend execute frappe.db.set_single_value --args '["PayPal Settings", "sync_enabled", 0]'
  copy_compose exec -T backend bench --site frontend execute frappe.db.sql --args '["update `tabEmail Account` set enable_outgoing = 0, enable_incoming = 0"]'
  copy_compose exec -T backend bench --site frontend execute frappe.db.sql --args '["update `tabWebhook` set enabled = 0"]'
  # The navbar brand: the app name starts with COPY, so the copy cannot be mistaken for live.
  copy_compose exec -T backend bench --site frontend execute frappe.db.set_single_value --args '["Website Settings", "app_name", "COPY - finance copy, not live"]'
}

# The copy's own Administrator password (the restore brings the live hash back). Made once, mode 600.
set_admin_password() {
  if [ ! -f "$PW_FILE" ]; then
    ( umask 077; openssl rand -base64 24 > "$PW_FILE" )
    echo "made $PW_FILE (mode 600)"
  fi
  copy_compose exec -T backend bench --site frontend set-admin-password "$(cat "$PW_FILE")"
}

up() {
  require_image "$TAG"
  check_memory "$TAG"
  live_backup
  export COPY_IMAGE="frappe-finance-custom:$TAG"
  copy_compose up -d --scale scheduler=0
  # create-site makes the site the restore replaces; wait for it to finish (a few minutes on the first start).
  CS=$(copy_compose ps -aq create-site)
  if [ -z "$CS" ] || [ "$(docker wait "$CS")" != "0" ]; then
    refuse "create-site did not finish; see: docker compose -p $PROJECT -f pwd.yml -f finance-copy.yml logs create-site"
  fi
  restore_copy
  safety
  set_admin_password
  echo "copy up on 127.0.0.1:8815 from a fresh live backup, image frappe-finance-custom:$TAG; not on the tailnet"
}

refresh() {
  copy_running
  live_backup
  restore_copy
  safety
  set_admin_password
  echo "copy refreshed from a fresh live backup"
}

down() {
  copy_compose down
  echo "copy stopped; its volumes are kept"
}

remove() {
  copy_compose down -v
  rm -rf "${PRIVATE:?}/copy-restore" "${PRIVATE:?}/.erpnext-admin-copy"
  echo "copy removed: containers, volumes and the staged backups"
}

case "$CMD" in
  up) up ;;
  refresh) refresh ;;
  down) down ;;
  remove) remove ;;
esac
