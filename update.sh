#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ -f .env ]] || { echo ".env not found; run install.sh first" >&2; exit 1; }
docker compose build --pull
docker compose run --rm bootstrap
docker compose up -d --remove-orphans bot worker admin
docker image prune -f
docker compose ps
