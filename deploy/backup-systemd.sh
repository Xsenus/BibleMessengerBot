#!/usr/bin/env bash
# Encrypted backups for the native systemd installation.
set -Eeuo pipefail
umask 077
base=/opt/bible-messenger-bot
[[ $EUID == 0 ]] || exit 64
exec 9>/run/lock/bible-auto-deploy.lock
flock -w 3600 9

release=$(readlink -e "$base/current")
[[ $release =~ ^/opt/bible-messenger-bot/releases/[0-9a-f]{40}$ ]]
python="$release/.venv/bin/python"
[[ -x $python ]]
cd "$release"
export PYTHONPATH="$release"

# Fail before creating a local dump when cloud backup is not configured.
"$python" - <<'PY'
from app.services.cloud_backup import CloudSettings, encryption_bytes
settings = CloudSettings.from_env()
if not settings.enabled:
    raise SystemExit("Cloud backup is not configured")
encryption_bytes(settings.encryption_key)
PY

mkdir -p "$base/backups"
name="$base/backups/biblebot-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
tmp="$name.part"
trap 'rm -f -- "$tmp"' EXIT

/usr/bin/python3 /usr/local/libexec/bible-systemd-ops backup > "$tmp"
[[ -s $tmp ]]
pg_restore --list "$tmp" > /dev/null
mv -- "$tmp" "$name"
sha256sum "$name" > "$name.sha256"
printf 'Database backup: %s\n' "$name"

# A zero CLI exit code can also mean "disabled". Require a verified S3 receipt.
"$python" -m app.backup_admin upload-db --file "$name" | "$python" -c '
import json, sys
result = json.load(sys.stdin)
if result.get("status") != "verified":
    raise SystemExit("Cloud backup was not verified")
print(json.dumps(result))
'
"$python" -m app.backup_admin prune-local --directory "$base/backups"
printf 'Backup complete: verified S3 archive and local retention\n'
