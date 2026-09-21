#!/usr/bin/env bash
# Safe fresh install or upgrade. Existing .env/database passwords are never regenerated.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
log() { printf '\n%s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "\nInstallation stopped. No success is claimed. Inspect the last error and runtime-evidence/. Existing data was not deleted.\n" >&2' ERR
if [[ $EUID -ne 0 ]]; then
  exec sudo --preserve-env=BOT_TOKEN,BIBLE_PROFILE,MAX_EDITIONS_PER_LANGUAGE,REQUIRED_LANGUAGES bash "$0" "$@"
fi
[[ -f /etc/os-release ]] || die "Unsupported OS"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) die "Use Docker Compose manually on this OS" ;; esac
if [[ -f MANIFEST.sha256 ]]; then sha256sum --quiet -c MANIFEST.sha256; fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg openssl
  install -m0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$(dpkg --print-architecture)" "$ID" "${VERSION_CODENAME:?OS codename missing}" >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
command -v openssl >/dev/null || { apt-get update; apt-get install -y openssl; }
mkdir -p runtime-evidence backups
if [[ -f .env ]]; then
  log "Keeping existing .env and all database credentials."
  chmod 600 .env
else
  if [[ -n "$(docker volume ls -q --filter name='^bible-messenger-bot_postgres_data$')" ]]; then
    die "Existing database volume found but .env is missing. Restore the original .env; do not generate a different password."
  fi
  TOKEN="${BOT_TOKEN:-}"
  if [[ -z "$TOKEN" ]]; then read -r -s -p 'Telegram bot token: ' TOKEN; printf '\n'; fi
  [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "Invalid token format"
  PROFILE="${BIBLE_PROFILE:-extended}"
  case "$PROFILE" in core|extended|all-open|none) ;; *) die "Invalid profile" ;; esac
  MAX_EDITIONS="${MAX_EDITIONS_PER_LANGUAGE:-2}"
  [[ "$MAX_EDITIONS" =~ ^[0-9]+$ ]] && (( MAX_EDITIONS>=1 && MAX_EDITIONS<=20 )) || die "Invalid edition limit"
  REQUIRED="${REQUIRED_LANGUAGES:-rus,eng}"
  [[ "$REQUIRED" =~ ^[a-z,]*$ ]] || die "Invalid required language list"
  PG_PASS="$(openssl rand -hex 24)"
  ADMIN_KEY="$(openssl rand -hex 32)"
  CLAIM="$(openssl rand -hex 16)"
  cat >.env <<ENV
BOT_TOKEN=${TOKEN}
OWNER_CLAIM_CODE=${CLAIM}
POSTGRES_DB=biblebot
POSTGRES_USER=biblebot
POSTGRES_PASSWORD=${PG_PASS}
DATABASE_URL=postgresql://biblebot:${PG_PASS}@postgres:5432/biblebot
ADMIN_API_KEY=${ADMIN_KEY}
ADMIN_HOST_PORT=8080
BIBLE_PROFILE=${PROFILE}
MAX_EDITIONS_PER_LANGUAGE=${MAX_EDITIONS}
REQUIRED_LANGUAGES=${REQUIRED}
IMPORT_ON_START=true
ALLOW_RESTRICTED_LICENSES=false
ALLOW_UNKNOWN_LICENSES=false
SOURCE_CACHE_DIR=/app/cache
DEFAULT_TIMEZONE=Europe/Amsterdam
DEFAULT_SEND_TIME=09:00
WORKER_POLL_SECONDS=3
TELEGRAM_GLOBAL_RATE_PER_SECOND=20
TELEGRAM_CHAT_RATE_PER_SECOND=1
MAX_MESSAGE_LENGTH=3900
LOG_LEVEL=INFO
ENV
  chmod 600 .env
fi
# Validate silently: a rendered Compose configuration can contain secrets.
docker compose config --quiet
log 'Building image and checking installed dependencies.'
docker compose build --pull bootstrap
# Back up a running existing database before stopping any application service.
if docker compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
  bash backup.sh
fi
docker compose stop bot worker admin || true
docker compose up -d postgres
for _ in $(seq 1 90); do
  if docker compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then break; fi
  sleep 2
done
docker compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
log 'Running tests against installed libraries and an isolated temporary PostgreSQL database.'
docker compose run --rm --no-deps -e RUN_DB_TESTS=1 bootstrap \
  python -m pytest -o addopts= -q -p no:cacheprovider | tee runtime-evidence/tests.txt
log 'Applying migrations, downloading licensed editions and checking actual database rows.'
docker compose up --no-deps --force-recreate --abort-on-container-exit --exit-code-from bootstrap bootstrap \
  | tee runtime-evidence/bootstrap.txt
docker compose run --rm --no-deps bootstrap python -m app.cli audit \
  >runtime-evidence/database-audit.json
docker compose run --rm --no-deps bootstrap python -m pip freeze >runtime-evidence/dependencies.txt
log 'Starting only after successful tests and corpus audit.'
docker compose up -d --no-deps bot worker admin
ready=false
for _ in $(seq 1 90); do
  if docker compose exec -T admin curl -fsS http://127.0.0.1:8080/ready >/dev/null 2>&1 \
    && docker compose exec -T bot python -m app.healthcheck bot \
    && docker compose exec -T worker python -m app.healthcheck worker; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || die "Runtime health checks failed; inspect docker compose logs bot worker admin"
docker compose ps
log 'Local installation checks passed. Start the bot in Telegram and use /settings.'
# Display local secrets only on the controlling terminal, never in evidence logs.
CLAIM="$(sed -n 's/^OWNER_CLAIM_CODE=//p' .env)"
printf '\nOwner claim (send privately to your bot): /claim %s\n' "$CLAIM"
printf 'Admin: SSH tunnel to 127.0.0.1:8080, /admin, login admin; password is ADMIN_API_KEY in .env.\n'
printf 'Actual editions, missing languages and structural results: runtime-evidence/database-audit.json\n'
