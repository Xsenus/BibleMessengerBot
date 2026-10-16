#!/usr/bin/env bash
# Ограниченный SSH-вход: разрешён только полный SHA, без shell, SCP и перенаправлений.
set -Eeuo pipefail
[[ "${SSH_ORIGINAL_COMMAND:-}" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected one full commit SHA.' >&2; exit 64; }
exec /usr/bin/sudo -n /usr/local/sbin/bible-auto-deploy submit "$SSH_ORIGINAL_COMMAND"
