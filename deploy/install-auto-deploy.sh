#!/usr/bin/env bash
# Установить отдельного SSH-пользователя и root-сценарий; принимает только файл публичного Ed25519-ключа.
set -Eeuo pipefail
[[ "$(id -u)" == 0 && $# == 1 ]] || { echo 'Usage: sudo bash deploy/install-auto-deploy.sh PUBLIC_KEY_FILE' >&2; exit 64; }
source_dir="$(dirname "$(readlink -f "$0")")"
public_key="$(awk 'NR==1 {print $1 " " $2}' "$1")"
[[ "$public_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]]
ssh-keygen -lf "$1" >/dev/null
id bibledeploy >/dev/null 2>&1 || useradd --create-home --home-dir /var/lib/bible-deploy --shell /bin/bash bibledeploy
install -d -m 755 /usr/local/libexec
install -m 755 "$source_dir/ssh-deploy.sh" /usr/local/libexec/bible-deploy-ssh
install -m 755 "$source_dir/auto-deploy.sh" /usr/local/sbin/bible-auto-deploy
install -m 755 "$source_dir/write-deployment-status.py" /usr/local/libexec/bible-deploy-status
install -m 755 "$source_dir/notify-deployment.py" /usr/local/libexec/bible-deploy-notify
install -d -m 755 /usr/local/share/bible-messenger-bot
install -m 644 "$source_dir/deployment-failures.json" /usr/local/share/bible-messenger-bot/deployment-failures.json
install -d -m 755 /opt/bible-messenger-bot/runtime-evidence/deploy-status
install -d -m 700 -o bibledeploy -g bibledeploy /var/lib/bible-deploy/.ssh
printf 'restrict,command="/usr/local/libexec/bible-deploy-ssh" %s\n' "$public_key" > /var/lib/bible-deploy/.ssh/authorized_keys
chown bibledeploy:bibledeploy /var/lib/bible-deploy/.ssh/authorized_keys
chmod 600 /var/lib/bible-deploy/.ssh/authorized_keys
sudoers="$(mktemp)"
trap 'rm -f "$sudoers"' EXIT
printf 'bibledeploy ALL=(root) NOPASSWD: /usr/local/sbin/bible-auto-deploy submit *\n' > "$sudoers"
visudo -cf "$sudoers"
install -m 440 "$sudoers" /etc/sudoers.d/bible-auto-deploy
echo 'Restricted deployment account installed. SSH daemon configuration was not changed.'
