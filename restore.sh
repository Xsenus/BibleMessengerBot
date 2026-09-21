#!/usr/bin/env bash
# Explicitly destructive restore; rollback on pg_restore error, never delete Docker volumes.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
[[ $# == 2 && "$2" == '--confirm-replace-data' && -f "$1" ]] || {
  echo 'Usage: bash restore.sh backups/FILE.dump --confirm-replace-data' >&2; exit 2;
}
file="$(readlink -f "$1")"
docker compose up -d postgres
docker compose exec -T postgres pg_restore --list <"$file" >/dev/null
bash backup.sh
docker compose stop bot worker admin
trap 'echo "Restore stopped; services remain paused. Original backup is in backups/." >&2' ERR
docker compose exec -T postgres sh -c 'exec pg_restore --clean --if-exists --single-transaction --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"$file"
docker compose run --rm --no-deps bootstrap python -m app.cli seed
docker compose run --rm --no-deps bootstrap python -m app.cli audit
docker compose up -d --no-deps bot worker admin
