#!/bin/bash
# shellcheck disable=SC2016 # the awk programs given to pick are single-quoted on purpose: $2 is awk's field, not the shell's
# One-shot restore of the dev site from the encrypted backup set (finance/docs/hetzner-dev-handoff.md, section 3).
# Two services in the Compose profile `restore` run it, never a plain `up`:
#   db     loads the database into db-data, with a MariaDB that listens on its socket only (network_mode: none);
#   files  writes sites/frontend: site_config.json and both file archives.
# restore-files depends on restore-db, so `run --rm restore-files` does both, in that order.
# usage: dev-restore.sh db|files
#
# Nothing is written before every check has passed: the ciphertext's checksum, the pinned age binary, the manifest,
# and the checksum of each of the four members. No plaintext reaches a file: each member is streamed from age into
# tar and on into its consumer, so the archive is decrypted again for every read.
set -euo pipefail

die() {
  echo "restore: $*" >&2
  exit 1
}

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
*) die "no pinned age binary for $(uname -m)" ;;
esac
AGE_SHA256=${DEV_RESTORE_AGE_SHA256:-$AGE_SHA256}

[ -f "$CIPHERTEXT" ] && [ -f "$STAGING/finance-dev-backup.tar.age.sha256" ] ||
  die "staging has no finance-dev-backup.tar.age with its .sha256"
[ -x "$AGE" ] || die "staging has no executable tools/age"
[ -s "$IDENTITY" ] || die "no age identity at $IDENTITY"
printf '%s  %s\n' "$AGE_SHA256" "$AGE" | sha256sum -c - >/dev/null ||
  die "tools/age does not match the pinned age v1.3.2 hash for $(uname -m)"
(cd "$STAGING" && sha256sum -c finance-dev-backup.tar.age.sha256) ||
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

DB_LINE=$(pick database '$2 ~ /-database\.sql\.gz$/')
CONF_LINE=$(pick site_config '$2 ~ /-site_config_backup\.json$/')
FILES_LINE=$(pick public_files '$2 ~ /-files\.tar$/ && $2 !~ /-private-files\.tar$/')
PRIV_LINE=$(pick private_files '$2 ~ /-private-files\.tar$/')
read -r DB_HASH DB_MEMBER <<<"$DB_LINE"
read -r CONF_HASH CONF_MEMBER <<<"$CONF_LINE"
read -r FILES_HASH FILES_MEMBER <<<"$FILES_LINE"
read -r PRIV_HASH PRIV_MEMBER <<<"$PRIV_LINE"

# Every member is checked before the first write: present in the archive, and of the checksum the manifest gives.
check_member() {
  listed "$2" || die "$2 is listed in SHA256SUMS but missing from the archive"
  local got
  got=$(member "$2" | sha256sum) || die "cannot read $2 from the archive"
  [ "${got%% *}" = "$1" ] || die "$2 does not match its checksum in SHA256SUMS"
  echo "restore: verified $2 sha256 ${got%% *}"
}
check_member "$DB_HASH" "$DB_MEMBER"
check_member "$CONF_HASH" "$CONF_MEMBER"
check_member "$FILES_HASH" "$FILES_MEMBER"
check_member "$PRIV_HASH" "$PRIV_MEMBER"
CONF=$(member "$CONF_MEMBER") || die "cannot read the site configuration"

# A plain string value of site_config.json, whatever the layout of the file. Frappe writes names and passwords without
# escapes, so a value that needs one (a quote or a backslash) is refused rather than read wrongly.
json_value() {
  local value
  value=$(sed -n 's/.*"'"$1"'": "\([^"\\]*\)".*/\1/p' <<<"$CONF" | head -n 1)
  [ -n "$value" ] || die "site_config.json has no plain $1"
  printf '%s\n' "$value"
}

# text as a single-quoted SQL literal: the server reads a backslash as an escape unless told otherwise
sql_string() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\'/\'\'}
  printf "'%s'" "$s"
}

restore_db() {
  # Everything is validated before the datadir is touched: a refusal must leave the volume as it was.
  [ -n "${DB_ROOT_PASSWORD:-}" ] || die "DB_ROOT_PASSWORD is not set"
  DB_NAME=$(json_value db_name)
  DB_PASSWORD=$(json_value db_password)
  # Both are written into SQL unquoted (the name) or inside a literal (the password): only plain forms pass.
  [[ "$DB_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "db_name is not a plain identifier"
  [[ "$DB_PASSWORD" =~ ^[A-Za-z0-9_.+/=-]+$ ]] || die "db_password has characters this restore does not write"

  if [ -n "$(ls -A "$DATADIR")" ]; then
    [ "$OVERWRITE" = 1 ] || die "$DATADIR is not empty: refusing to replace the database (FINANCE_DEV_RESTORE_OVERWRITE=1 replaces it)"
    find "$DATADIR" -mindepth 1 -delete
  fi

  SOCKDIR=$(mktemp -d)
  chmod 0777 "$SOCKDIR" # mariadbd runs as mysql and creates its socket here
  SOCKET=$SOCKDIR/mysqld.sock
  mariadb-install-db --user=mysql --datadir="$DATADIR" --skip-test-db >"$SOCKDIR/install.log" 2>&1 ||
    {
      cat "$SOCKDIR/install.log" >&2
      die "mariadb-install-db failed"
    }
  mariadbd --user=mysql --datadir="$DATADIR" --socket="$SOCKET" --skip-networking &
  PID=$!
  for _ in $(seq 60); do
    mariadb-admin --socket="$SOCKET" ping >/dev/null 2>&1 && break
    sleep 1
  done
  mariadb-admin --socket="$SOCKET" ping >/dev/null 2>&1 || die "mariadbd did not answer on its socket"

  mariadb_root() {
    mariadb --socket="$SOCKET" --user=root "$@"
  }
  {
    echo "CREATE DATABASE $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    echo "CREATE USER '$DB_NAME'@'%' IDENTIFIED BY '$DB_PASSWORD';"
    echo "GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_NAME'@'%';"
  } | mariadb_root || die "could not create the site database and its user"
  # The client's errors can quote row values, so they go to a file and only their count is printed.
  if ! member "$DB_MEMBER" | gzip -dc | mariadb_root --database="$DB_NAME" 2>"$SOCKDIR/import.err"; then
    die "database import failed: $(grep -c . "$SOCKDIR/import.err" || true) error line(s), not shown"
  fi
  # The root account gets the dev root password here, as the db service would set it on a fresh volume.
  printf "ALTER USER 'root'@'localhost' IDENTIFIED BY %s;\n" "$(sql_string "$DB_ROOT_PASSWORD")" | mariadb_root ||
    die "could not set the root password"
  MYSQL_PWD=$DB_ROOT_PASSWORD mariadb-admin --socket="$SOCKET" --user=root shutdown || die "mariadbd did not shut down"
  wait "$PID" || die "mariadbd exited with an error"
  PID=
  echo "restore: database restored into $DATADIR"
}

restore_files() {
  if [ -e "$SITES/$SITE" ] || [ -n "$(find "$SITES" -mindepth 2 -maxdepth 2 -name site_config.json)" ]; then
    [ "$OVERWRITE" = 1 ] || die "$SITES already holds a site: refusing to replace it (FINANCE_DEV_RESTORE_OVERWRITE=1 replaces $SITE)"
    rm -rf -- "${SITES:?}/$SITE"
  fi
  SITE_DIR=$SITES/$SITE
  mkdir -p "$SITE_DIR/public" "$SITE_DIR/private"
  # The archives hold sites/<site>/public/files and sites/<site>/private/files, as Frappe writes them; stripping two
  # levels puts them where bench restore extracts them (installer.py extract_files).
  (
    umask 077
    member "$CONF_MEMBER" >"$SITE_DIR/site_config.json.partial"
  ) || die "cannot write site_config.json"
  mv "$SITE_DIR/site_config.json.partial" "$SITE_DIR/site_config.json"
  member "$FILES_MEMBER" | tar -xf - --strip-components=2 -C "$SITE_DIR" || die "cannot extract the public files"
  member "$PRIV_MEMBER" | tar -xf - --strip-components=2 -C "$SITE_DIR" || die "cannot extract the private files"
  echo "restore: site restored into $SITE_DIR"
}

case "$MODE" in
db) restore_db ;;
files) restore_files ;;
esac
