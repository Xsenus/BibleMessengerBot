#!/usr/bin/env bash
# Install the application-only systemd overrides for the native release layout.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 ]] || exit 64
here=$(dirname "$(readlink -f "$0")")
base=/opt/bible-messenger-bot
active=$(readlink -e "$base/current")
[[ $active =~ ^/opt/bible-messenger-bot/releases/[0-9a-f]{40}$ && -x $base/venv/bin/python ]]
units=(admin max-web bot maxbot worker composer illustrator speaker)
commands=(
    '-m uvicorn app.web.main:app --host 127.0.0.1 --port 8420 --no-proxy-headers'
    '-m uvicorn app.maxbot.web:app --host 127.0.0.1 --port 8421 --no-proxy-headers'
    '-m app.bot.main'
    '-m app.maxbot.main'
    '-m app.worker.main'
    '-m app.worker.composer'
    '-m app.worker.illustrator'
    '-m app.worker.speaker'
)
for service in "${units[@]}"; do
    unit=bible-messenger-$service.service
    [[ $(systemctl show "$unit" -p WorkingDirectory --value) == "$base/current" ]]
    [[ $(systemctl show "$unit" -p User --value) == biblemessenger ]]
done
# Keep the original interpreter available for the first rollback.
if [[ ! -e $active/.venv ]]; then ln -s "$base/venv" "$active/.venv"; fi
bash "$here/install-auto-deploy.sh" "$1" --systemd
for index in "${!units[@]}"; do
    directory="/etc/systemd/system/bible-messenger-${units[$index]}.service.d"
    install -d -m 755 "$directory"
    printf '[Service]\nExecStart=\nExecStart=/usr/local/libexec/bible-release-python %s\n' "${commands[$index]}" \
        > "$directory/80-release-python.conf"
    if [[ ${units[$index]} == admin ]]; then
        printf 'Environment=BIBLE_DEPLOY_STATUS_FILE=%s/runtime-evidence/deploy-status/status.json\n' "$base" \
            >> "$directory/80-release-python.conf"
    fi
done
systemctl daemon-reload
