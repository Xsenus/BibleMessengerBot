#!/usr/bin/env bash
# Transaction-consistent PostgreSQL custom archive. Contains private user/chat data.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p backups
name="backups/biblebot-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
tmp="${name}.part"
trap 'rm -f "$tmp"' EXIT
docker compose exec -T postgres sh -c 'exec pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$tmp"
[[ -s "$tmp" ]]
mv "$tmp" "$name"
sha256sum "$name" >"${name}.sha256"
printf 'Database backup: %s\n' "$name"
