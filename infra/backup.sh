#!/usr/bin/env bash
# Nightly Postgres dump. Keeps the last 7 daily dumps. Exits non-zero if the dump is empty.
set -euo pipefail
# Dumps hold user data: files 600, folder 700.
umask 077

COMPOSE_DIR="${COMPOSE_DIR:-/opt/pubmedgemini}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/pubmedgemini}"
KEEP=7

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cd "$COMPOSE_DIR"

out="$BACKUP_DIR/$(date +%F).dump"
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT

docker compose exec -T db pg_dump -U postgres -Fc postgres > "$tmp"

if [ ! -s "$tmp" ]; then
  echo "backup failed: dump is empty" >&2
  exit 1
fi

mv "$tmp" "$out"

# Names are YYYY-MM-DD.dump, so the glob lists them oldest first.
shopt -s nullglob
dumps=("$BACKUP_DIR"/????-??-??.dump)
if [ "${#dumps[@]}" -gt "$KEEP" ]; then
  rm -f -- "${dumps[@]:0:${#dumps[@]}-KEEP}"
fi
echo "backup ok: $out"
