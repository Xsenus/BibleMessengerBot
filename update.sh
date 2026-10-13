#!/usr/bin/env bash
# Update only previously running services, preserving .env and all data.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -f .env ]] || { echo 'Copy the original private .env into this directory before upgrading.' >&2; exit 2; }
exec bash deploy/rolling-update.sh "$@"
