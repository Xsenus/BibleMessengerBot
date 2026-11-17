#!/usr/bin/env bash
# Resolve the interpreter once, so a current symlink switch cannot redirect a running process.
set -Eeuo pipefail
release=$(pwd -P)
[[ $release =~ ^/opt/bible-messenger-bot/releases/[0-9a-f]{40}$ ]]
venv=$(readlink -e "$release/.venv")
exec "$venv/bin/python" "$@"
