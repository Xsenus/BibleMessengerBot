#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="backups/biblebot-${stamp}.sql.gz"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-biblebot}" -d "${POSTGRES_DB:-biblebot}" --clean --if-exists | gzip -9 > "$archive"
sha256sum "$archive" > "${archive}.sha256"
chmod 600 "$archive" "${archive}.sha256"
echo "$archive"
