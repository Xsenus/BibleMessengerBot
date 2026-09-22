#!/usr/bin/env bash
# Restore only into a uniquely named disposable database; never replace production.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
[[ $# == 1 && -f "$1" ]] || { echo 'Usage: bash verify_backup.sh backups/FILE.dump' >&2; exit 2; }
file="$(readlink -f "$1")"
docker compose exec -T postgres pg_restore --list <"$file" >/dev/null
name="biblebot_verify_$(date -u +%Y%m%d%H%M%S)_$$"
created=false
cleanup() {
  if [[ "$created" == true && "$name" =~ ^biblebot_verify_[0-9]+_[0-9]+$ ]]; then
    docker compose exec -T -e VERIFY_DATABASE="$name" postgres sh -c 'dropdb -U "$POSTGRES_USER" "$VERIFY_DATABASE"'
  fi
}
trap cleanup EXIT
docker compose exec -T -e VERIFY_DATABASE="$name" postgres sh -c 'createdb -U "$POSTGRES_USER" "$VERIFY_DATABASE"'
created=true
docker compose exec -T -e VERIFY_DATABASE="$name" postgres sh -c 'pg_restore --single-transaction --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$VERIFY_DATABASE"' <"$file"
docker compose run --rm --no-deps -T -e VERIFY_DATABASE="$name" bootstrap python - <<'PY'
import asyncio
import json
import os
from urllib.parse import urlsplit, urlunsplit
import asyncpg
from app.services.verification import verify_database

async def main():
    parsed = urlsplit(os.environ['DATABASE_URL'])
    dsn = urlunsplit(parsed._replace(path='/' + os.environ['VERIFY_DATABASE']))
    connection = await asyncpg.connect(dsn)
    try:
        report = await verify_database(connection, profile=os.getenv('BIBLE_PROFILE', 'core'), full=True)
        print(json.dumps({key: report[key] for key in ('status', 'edition_count', 'languages', 'errors')}))
        if report['status'] != 'passed':
            raise SystemExit(1)
    finally:
        await connection.close()

asyncio.run(main())
PY
echo 'Backup restored and corpus verified in a disposable database. Production was not modified.'
