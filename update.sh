#!/usr/bin/env bash
# Reuse the idempotent installer, preserving .env. Run from the new release directory.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -f .env ]] || { echo 'Copy the original private .env into this directory before upgrading.' >&2; exit 2; }
exec bash install.sh "$@"
