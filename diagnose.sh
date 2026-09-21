#!/usr/bin/env bash
# No rendered Compose/env output: diagnostics must not expose credentials.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
docker compose ps
docker compose logs --tail=80 bot worker admin bootstrap
docker compose run --rm --no-deps bootstrap python -m app.cli audit
