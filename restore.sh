#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
file="${1:-}"
[[ -f "$file" ]] || { echo "Usage: $0 backups/file.sql.gz" >&2; exit 1; }
read -r -p "This replaces the current database. Type RESTORE: " answer
[[ "$answer" == "RESTORE" ]] || exit 1
docker compose stop bot worker admin
gzip -dc "$file" | docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-biblebot}" -d "${POSTGRES_DB:-biblebot}"
docker compose start bot worker admin
