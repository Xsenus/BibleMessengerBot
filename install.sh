#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

log() { printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "Run as root or install sudo"
  exec sudo --preserve-env=BOT_TOKEN,BIBLE_PROFILE,MAX_EDITIONS_PER_LANGUAGE,ADMIN_LISTEN_IP,ADMIN_HOST_PORT bash "$0" "$@"
fi

[[ -f /etc/os-release ]] || die "Unsupported operating system"
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) die "Automatic installer supports Ubuntu/Debian. Use Docker Compose manually on ${ID:-unknown}." ;;
esac

install_docker() {
  log "Installing Docker Engine and Compose plugin"
  apt-get update
  apt-get install -y ca-certificates curl gnupg openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  local codename="${VERSION_CODENAME:-}"
  [[ -n "$codename" ]] || codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-bookworm}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install_docker
fi

TOKEN="${BOT_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  read -r -s -p "Telegram Bot Token from @BotFather: " TOKEN
  printf '\n'
fi
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "BOT_TOKEN format looks invalid"

PROFILE="${BIBLE_PROFILE:-extended}"
case "$PROFILE" in core|extended|all-open|none) ;; *) die "Invalid BIBLE_PROFILE: $PROFILE" ;; esac
MAX_EDITIONS="${MAX_EDITIONS_PER_LANGUAGE:-2}"
[[ "$MAX_EDITIONS" =~ ^[0-9]+$ ]] || die "MAX_EDITIONS_PER_LANGUAGE must be an integer"
(( MAX_EDITIONS >= 1 && MAX_EDITIONS <= 20 )) || die "MAX_EDITIONS_PER_LANGUAGE must be between 1 and 20"
TZ_NAME="$(cat /etc/timezone 2>/dev/null || true)"
[[ -n "$TZ_NAME" ]] || TZ_NAME="Europe/Amsterdam"
POSTGRES_PASSWORD="$(openssl rand -hex 24)"
ADMIN_API_KEY="$(openssl rand -hex 32)"
OWNER_CLAIM_CODE="$(openssl rand -hex 16)"
ADMIN_LISTEN="${ADMIN_LISTEN_IP:-127.0.0.1}"
ADMIN_PORT="${ADMIN_HOST_PORT:-8080}"

cat > .env <<ENV
BOT_TOKEN=${TOKEN}
OWNER_CLAIM_CODE=${OWNER_CLAIM_CODE}
POSTGRES_DB=biblebot
POSTGRES_USER=biblebot
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DATABASE_URL=postgresql://biblebot:${POSTGRES_PASSWORD}@postgres:5432/biblebot
ADMIN_API_KEY=${ADMIN_API_KEY}
ADMIN_BIND=0.0.0.0
ADMIN_PORT=8080
ADMIN_LISTEN_IP=${ADMIN_LISTEN}
ADMIN_HOST_PORT=${ADMIN_PORT}
BIBLE_PROFILE=${PROFILE}
MAX_EDITIONS_PER_LANGUAGE=${MAX_EDITIONS}
IMPORT_ON_START=true
ALLOW_RESTRICTED_LICENSES=false
ALLOW_UNKNOWN_LICENSES=false
IMPORT_BATCH_SIZE=2000
DOWNLOAD_TIMEOUT_SECONDS=180
SOURCE_CACHE_DIR=/app/cache
DEFAULT_TIMEZONE=${TZ_NAME}
DEFAULT_SEND_TIME=09:00
WORKER_POLL_SECONDS=15
TELEGRAM_GLOBAL_RATE_PER_SECOND=20
TELEGRAM_CHAT_RATE_PER_SECOND=1
MAX_MESSAGE_LENGTH=3900
LOG_LEVEL=INFO
PUBLIC_BASE_URL=
ENV
chmod 600 .env

log "Building application image"
docker compose build --pull

log "Starting PostgreSQL"
docker compose up -d postgres
for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U biblebot -d biblebot >/dev/null 2>&1; then break; fi
  sleep 2
done
docker compose exec -T postgres pg_isready -U biblebot -d biblebot >/dev/null 2>&1 \
  || die "PostgreSQL did not become ready"

log "Creating schema and importing Bible editions (profile: ${PROFILE})"
docker compose run --rm bootstrap | tee bootstrap-result.json

log "Starting bot, scheduler, and admin panel"
docker compose up -d bot worker admin

log "Checking services"
sleep 3
docker compose ps
if command -v curl >/dev/null 2>&1; then
  curl -fsS "http://127.0.0.1:${ADMIN_PORT}/health" >/dev/null \
    || log "Admin health endpoint is not ready yet; inspect with: docker compose logs admin"
fi

cat <<INFO

Installation completed.

1. Open the Telegram bot and send:
   /claim ${OWNER_CLAIM_CODE}

2. Admin panel (bound locally for security):
   http://127.0.0.1:${ADMIN_PORT}/admin
   username: admin
   password: ${ADMIN_API_KEY}

   From your computer use an SSH tunnel:
   ssh -L ${ADMIN_PORT}:127.0.0.1:${ADMIN_PORT} USER@SERVER_IP

3. Useful commands:
   cd ${ROOT_DIR}
   docker compose ps
   docker compose logs -f bot worker
   python is not required on the VPS; all services run in Docker.

Secrets are stored in ${ROOT_DIR}/.env with mode 600.
INFO
