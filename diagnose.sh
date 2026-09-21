#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
echo "== Compose =="
docker compose version
docker compose config --quiet
echo "== Services =="
docker compose ps
echo "== Database =="
docker compose exec -T postgres pg_isready -U biblebot -d biblebot
echo "== Statistics =="
docker compose run --rm --no-deps bot python -m app.cli stats
echo "== Recent logs =="
docker compose logs --tail=80 bot worker admin bootstrap
