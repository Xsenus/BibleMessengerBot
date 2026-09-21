#!/usr/bin/env bash
# Host/offline checks only. PostgreSQL/aiogram integrations are explicitly skipped if unavailable.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ ! -f MANIFEST.sha256 ]] || sha256sum --quiet -c MANIFEST.sha256
python -m compileall -q app tests
for f in *.sh scripts/*.sh; do [[ ! -e "$f" ]] || bash -n "$f"; done
python -m pytest -o addopts= -q
