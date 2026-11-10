#!/usr/bin/env bash
# Nightly Postgres dump. Keeps 7 days. Exits non-zero if the dump is empty.
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/pubmedgemini}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/pubmedgemini}"

mkdir -p "$BACKUP_DIR"
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
find "$BACKUP_DIR" -name '*.dump' -type f -mtime +7 -delete
echo "backup ok: $out"
