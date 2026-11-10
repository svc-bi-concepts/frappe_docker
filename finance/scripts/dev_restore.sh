#!/bin/bash
# shellcheck disable=SC2016 # the awk programs given to pick are single-quoted on purpose: $2 is awk's field, not the shell's
# One-shot restore of the dev site from the encrypted backup set (finance/docs/hetzner-dev-handoff.md, section 3).
# Two services in the Compose profile `restore` run it, never a plain `up`:
#   db     loads the database into db-data, with a MariaDB that listens on its socket only (network_mode: none);
#   files  writes sites/frontend: a dev site_config.json and both file archives.
# restore-files depends on restore-db, so `run --rm restore-files` does both, in that order.
# usage: dev-restore.sh db|files
#
# Nothing is written before every check has passed: the ciphertext's checksum, the pinned age binary, the manifest,
# and the checksum of each of the four members. No decrypted staging files are written: each member is streamed from age into
# tar and on into its consumer, so the archive is decrypted again for every read.
#
# The backup's site configuration is checked like every member and then never read: it holds the source site's
# encryption_key and database credentials, and none of them belongs on dev (handoff doc, section 3). The site
# configuration written here is dev's own.
set -Eeuo pipefail
exec 3>&2 2>/dev/null

die() {
  echo "restore: $*" >&3
  exit 1
}

trap 'die "operation failed"' ERR

# A MariaDB started here is stopped on every exit, so a failed restore leaves no server behind.
PID=
SOCKDIR=
cleanup() {
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  if [ -n "$SOCKDIR" ]; then
    rm -rf -- "$SOCKDIR"
  fi
}
trap cleanup EXIT

MODE=${1:-}
case "$MODE" in
db | files) ;;
*) die "usage: dev-restore.sh db|files" ;;
esac

# The staging and identity mounts are read-only (the "restore contract" in the handoff doc). The DEV_RESTORE_*
# paths exist for the host tests; the Compose file sets none of them.
STAGING=${DEV_RESTORE_STAGING:-/restore/staging}
IDENTITY=${DEV_RESTORE_IDENTITY:-/restore/identity/identity.txt}
DATADIR=${DEV_RESTORE_DATADIR:-/var/lib/mysql}
SITES=${DEV_RESTORE_SITES:-/home/frappe/frappe-bench/sites}
SITE=frontend
CIPHERTEXT=$STAGING/finance-dev-backup.tar.age
AGE=$STAGING/tools/age

case "${FINANCE_DEV_RESTORE_OVERWRITE:-0}" in
0) OVERWRITE=0 ;;
1) OVERWRITE=1 ;;
*) die "FINANCE_DEV_RESTORE_OVERWRITE must be 0 or 1" ;;
esac

# age v1.3.2 from its GitHub release: the tarball's sha256 equals the release asset digest, and these are the hashes of
# the binaries inside. The staging volume carries the build for the host's architecture as tools/age. Nothing is
# downloaded at run time. DEV_RESTORE_AGE_SHA256 is for the host tests only.
case "$(uname -m)" in
x86_64 | amd64) AGE_SHA256=eb7dd1b518f0a307c99cd97782623c5321da049154b04acd2d98d21aa7bc9b2c ;;
aarch64 | arm64) AGE_SHA256=41b072352f4561018949623c674d16ef704019b9108a9bbdbd21292efebfc94f ;;
*) die "no pinned age binary for this architecture" ;;
esac
AGE_SHA256=${DEV_RESTORE_AGE_SHA256:-$AGE_SHA256}

[ -f "$CIPHERTEXT" ] && [ -f "$STAGING/finance-dev-backup.tar.age.sha256" ] ||
  die "staging has no finance-dev-backup.tar.age with its .sha256"
[ -x "$AGE" ] || die "staging has no executable tools/age"
[ -s "$IDENTITY" ] || die "no age identity supplied"
printf '%s  %s\n' "$AGE_SHA256" "$AGE" | sha256sum -c - >/dev/null ||
  die "tools/age does not match the pinned age v1.3.2 hash"
(cd "$STAGING" && sha256sum -c finance-dev-backup.tar.age.sha256 >/dev/null) ||
  die "the ciphertext does not match finance-dev-backup.tar.age.sha256"

# The decrypted archive on stdout; the plaintext exists only inside this pipe.
archive() {
  "$AGE" -d -i "$IDENTITY" "$CIPHERTEXT"
}

# One member of the decrypted archive on stdout.
member() {
  archive | tar -xOf - "$1"
}

LISTING=$(archive | tar -tf -) || die "cannot decrypt the archive: wrong identity, or a damaged file"
listed() {
  grep -qxF -- "$1" <<<"$LISTING"
}

listed SHA256SUMS || die "the archive has no SHA256SUMS manifest"
MANIFEST=$(member SHA256SUMS) || die "cannot read SHA256SUMS from the archive"
[ "$(awk 'NF { n++ } END { print n + 0 }' <<<"$MANIFEST")" = 4 ] ||
  die "SHA256SUMS must list exactly four members"

# The one manifest line ("<sha256> <member>") that the awk condition selects. The backup prefix is not checked: the
# four members must share one prefix, and the manifest is the only list of them.
pick() {
  local matches
  matches=$(awk "$2 { print }" <<<"$MANIFEST")
  case "$matches" in
  '') die "SHA256SUMS lists no $1" ;;
  *$'\n'*) die "SHA256SUMS lists more than one $1" ;;
  esac
  printf '%s\n' "$matches"
}

DB_LINE=$(pick 'database dump' '$2 ~ /-database\.sql\.gz$/')
CONF_LINE=$(pick 'site config' '$2 ~ /-site_config_backup\.json$/')
FILES_LINE=$(pick 'public files archive' '$2 ~ /-files\.tar$/ && $2 !~ /-private-files\.tar$/')
PRIV_LINE=$(pick 'private files archive' '$2 ~ /-private-files\.tar$/')
read -r DB_HASH DB_MEMBER <<<"$DB_LINE"
read -r CONF_HASH CONF_MEMBER <<<"$CONF_LINE"
read -r FILES_HASH FILES_MEMBER <<<"$FILES_LINE"
read -r PRIV_HASH PRIV_MEMBER <<<"$PRIV_LINE"

# Every member is checked before the first write: present in the archive, and of the checksum the manifest gives.
check_member() {
  listed "$2" || die "$3 is listed in SHA256SUMS but missing from the archive"
  local got
  got=$(member "$2" | sha256sum) || die "cannot read $3 from the archive"
  [ "${got%% *}" = "$1" ] || die "$3 does not match its checksum in SHA256SUMS"
  echo "restore: verified $3 sha256 ${got%% *}"
}
check_member "$DB_HASH" "$DB_MEMBER" "database dump"
check_member "$CONF_HASH" "$CONF_MEMBER" "site config"
check_member "$FILES_HASH" "$FILES_MEMBER" "public files archive"
check_member "$PRIV_HASH" "$PRIV_MEMBER" "private files archive"

# Dev's own database credentials for the site. The two services share no writable volume, so the password cannot be
# drawn at random in one and handed to the other: both derive it from the dev root password, which already opens
# every database of this resource. The name is fixed; a dump made by bench names no database.
[ -n "${DB_ROOT_PASSWORD:-}" ] || die "DB_ROOT_PASSWORD is not set"
DB_NAME=finance_dev
DB_PASSWORD=$(printf 'finance-dev site database user\n%s' "$DB_ROOT_PASSWORD" | sha256sum)
DB_PASSWORD=${DB_PASSWORD%% *}

# text as a single-quoted SQL literal: the server reads a backslash as an escape unless told otherwise
sql_string() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\'/\'\'}
  printf "'%s'" "$s"
}

restore_db() {
  # Everything is validated before the datadir is touched: a refusal must leave the volume as it was.
  if [ -n "$(ls -A "$DATADIR")" ]; then
    [ "$OVERWRITE" = 1 ] || die "database target is not empty: refusing to replace the database (FINANCE_DEV_RESTORE_OVERWRITE=1 replaces it)"
    find "$DATADIR" -mindepth 1 -delete
  fi

  SOCKDIR=$(mktemp -d)
  chmod 0777 "$SOCKDIR" # mariadbd runs as mysql and creates its socket here
  SOCKET=$SOCKDIR/mysqld.sock
  mariadb-install-db --user=mysql --datadir="$DATADIR" --skip-test-db >/dev/null ||
    die "mariadb-install-db failed"
  mariadbd --user=mysql --datadir="$DATADIR" --socket="$SOCKET" --skip-networking >/dev/null &
  PID=$!
  for _ in $(seq 60); do
    mariadb-admin --socket="$SOCKET" ping >/dev/null 2>&1 && break
    sleep 1
  done
  mariadb-admin --socket="$SOCKET" ping >/dev/null 2>&1 || die "mariadbd did not answer on its socket"

  mariadb_root() {
    mariadb --socket="$SOCKET" --user=root "$@" >/dev/null
  }
  {
    echo "CREATE DATABASE $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    echo "CREATE USER '$DB_NAME'@'%' IDENTIFIED BY '$DB_PASSWORD';"
    echo "GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_NAME'@'%';"
  } | mariadb_root || die "could not create the site database and its user"
  local errors
  if ! errors=$({ member "$DB_MEMBER" | gzip -dc | mariadb_root --database="$DB_NAME"; } 2>&1 |
    awk 'NF { n++ } END { print n + 0 }'); then
    die "database import failed: $errors error line(s), not shown"
  fi
  # The accounts the db service's entrypoint creates on a fresh volume, and skips on one that holds a database:
  # root with the dev root password, also from the network (MARIADB_ROOT_HOST defaults to %), and the healthcheck
  # users with their .my-healthcheck.cnf. Without them the db service's healthcheck.sh fails on every restored volume.
  # Port and socket are the image defaults, which the db service does not change.
  local root_password health_password host
  root_password=$(sql_string "$DB_ROOT_PASSWORD")
  health_password=$(head -c 32 /dev/urandom | sha256sum)
  health_password=${health_password%% *}
  {
    echo "CREATE USER 'root'@'%' IDENTIFIED BY $root_password;"
    echo "GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION;"
    echo "GRANT PROXY ON ''@'%' TO 'root'@'%' WITH GRANT OPTION;"
    for host in 127.0.0.1 ::1 localhost; do
      echo "CREATE USER 'healthcheck'@'$host' IDENTIFIED BY '$health_password';"
      echo "GRANT USAGE ON *.* TO 'healthcheck'@'$host';"
    done
    echo "ALTER USER 'root'@'localhost' IDENTIFIED BY $root_password;"
  } | mariadb_root || die "could not set up the root and healthcheck accounts"
  (
    umask 077
    printf '[mariadb-client]\nport=3306\nsocket=/run/mysqld/mysqld.sock\nuser=healthcheck\npassword=%s\n' \
      "$health_password" >"$DATADIR/.my-healthcheck.cnf"
  ) || die "cannot write .my-healthcheck.cnf"
  MYSQL_PWD=$DB_ROOT_PASSWORD mariadb-admin --socket="$SOCKET" --user=root shutdown >/dev/null || die "mariadbd did not shut down"
  wait "$PID" || die "mariadbd exited with an error"
  PID=
  echo "restore: database restored"
}

restore_files() {
  if [ -e "$SITES/$SITE" ] || [ -n "$(find "$SITES" -mindepth 2 -maxdepth 2 -name site_config.json)" ]; then
    [ "$OVERWRITE" = 1 ] || die "sites target already holds a site: refusing to replace it (FINANCE_DEV_RESTORE_OVERWRITE=1 replaces the dev site)"
    rm -rf -- "${SITES:?}/$SITE"
  fi
  SITE_DIR=$SITES/$SITE
  mkdir -p "$SITE_DIR/public" "$SITE_DIR/private"
  # The archives hold sites/<site>/public/files and sites/<site>/private/files, as Frappe writes them; stripping two
  # levels puts them where bench restore extracts them (installer.py extract_files).
  member "$FILES_MEMBER" | tar -xf - --strip-components=2 -C "$SITE_DIR" || die "cannot extract the public files"
  member "$PRIV_MEMBER" | tar -xf - --strip-components=2 -C "$SITE_DIR" || die "cannot extract the private files"
  # site_config.json comes last: dev-init starts the resource only when it exists, so a failed extraction above must
  # leave none behind.
  # No key of the backup's configuration is carried over. Without an encryption_key Frappe draws a new one on first
  # use, so passwords stored in the restored database do not decrypt on dev, which is intended. host_name and
  # disable_scheduler are the dev values of the off-switch sequence (handoff doc, section 2), which still runs.
  (
    umask 077
    printf '{\n "db_name": "%s",\n "db_password": "%s",\n "db_type": "mariadb",\n "disable_scheduler": 1,\n "host_name": "http://frontend:8080"\n}\n' \
      "$DB_NAME" "$DB_PASSWORD" >"$SITE_DIR/site_config.json.partial"
  ) || die "cannot write site_config.json"
  mv "$SITE_DIR/site_config.json.partial" "$SITE_DIR/site_config.json"
  echo "restore: site restored"
}

case "$MODE" in
db) restore_db ;;
files) restore_files ;;
esac
