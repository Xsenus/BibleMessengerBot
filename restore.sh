#!/usr/bin/env bash
# Explicitly destructive restore; rollback on pg_restore error, never delete Docker volumes.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
[[ ( $# == 2 || ( $# == 3 && "$3" == '--start-services' ) ) && "$2" == '--confirm-replace-data' && -f "$1" ]] || {
  echo 'Usage: bash restore.sh backups/FILE.dump --confirm-replace-data [--start-services]' >&2; exit 2;
}
file="$(readlink -f "$1")"
compose=(docker compose --profile '*')
running="$("${compose[@]}" ps --services --filter status=running)"
previous=()
for service in bot worker admin illustrator speaker composer max-web maxbot vkbot; do
  if grep -qx "$service" <<<"$running"; then previous+=("$service"); fi
done
mkdir -p runtime-evidence
printf '%s\n' "${previous[@]}" >runtime-evidence/restore-services.txt
docker compose up -d postgres
docker compose exec -T postgres pg_restore --list <"$file" >/dev/null
bash backup.sh
if (( ${#previous[@]} )); then "${compose[@]}" stop "${previous[@]}"; fi
trap 'echo "Restore stopped; services remain paused. Original backup is in backups/." >&2' ERR
docker compose exec -T postgres sh -c 'exec pg_restore --clean --if-exists --single-transaction --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"$file"
docker compose run --rm --no-deps bootstrap python -m app.cli migrate
docker compose run --rm --no-deps bootstrap python -m app.cli audit
docker compose run --rm --no-deps bootstrap python -m app.neural_audio_admin prepare
if [[ "${3:-}" == '--start-services' ]]; then
  if (( ${#previous[@]} )); then
    "${compose[@]}" up -d --no-deps --wait --wait-timeout 240 "${previous[@]}"
  fi
else
  echo 'Restore and audit passed. Delivery remains stopped: review restored outbox/history before starting bot and worker.'
  echo 'Previously running services are recorded in runtime-evidence/restore-services.txt; resume only those after review.'
fi
