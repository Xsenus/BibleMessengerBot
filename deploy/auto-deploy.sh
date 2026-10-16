#!/usr/bin/env bash
# Обновить проверенную main под блокировкой, сохранив БД, конфигурацию и образ отката.
set -Eeuo pipefail
umask 077
[[ $# == 2 && "$1" =~ ^(submit|run)$ && "$2" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected submit|run and full SHA.' >&2; exit 64; }
[[ "$(id -u)" == 0 ]] || { echo 'Root required.' >&2; exit 77; }
target="$2"
if [[ "$1" == submit ]]; then
  # systemd продолжает обновление при потере SSH/отмене GitHub job; exit code возвращается в Actions.
  exec systemd-run --unit="bible-auto-deploy-$target" --collect --wait --pipe \
    --property=RuntimeMaxSec=4800 --property=KillMode=control-group \
    /usr/local/sbin/bible-auto-deploy run "$target"
fi
project="${BIBLE_DEPLOY_PROJECT_DIR:-/opt/bible-messenger-bot}"
cd "$project"
exec 9>"${BIBLE_DEPLOY_LOCK:-/run/lock/bible-auto-deploy.lock}"
flock -w 4800 9
[[ -f .env && -z "$(git status --porcelain)" ]] || { echo 'Missing configuration or dirty checkout.' >&2; exit 1; }
branch="$(git branch --show-current)"
[[ -z "$branch" || "$branch" == main ]] || { echo 'Unexpected working branch.' >&2; exit 1; }
git fetch origin main
if [[ "$(git rev-parse origin/main)" != "$target" ]]; then
  echo 'SKIPPED: checked commit is no longer the head of main.'
  exit 0
fi
previous="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$previous" "$target"
compose=(docker compose --profile '*')
"${compose[@]}" config --quiet
free_kib="$(df -Pk . | awk 'NR==2 {print $4}')"
[[ "$free_kib" =~ ^[0-9]+$ && "$free_kib" -ge 4194304 ]] || { echo 'At least 4 GiB free disk space is required.' >&2; exit 1; }
state="$PWD/runtime-evidence/auto-deploy/$(date -u +%Y%m%dT%H%M%SZ)-${target:0:12}-$$"
mkdir -p "$state"
exec > >(tee -a "$state/deploy.log") 2>&1
echo "Deploy $previous -> $target"
sha256sum .env > "$state/environment.sha256"
cp docker-compose.yml "$state/previous-compose.yml"
"${compose[@]}" ps --format json > "$state/services-before.jsonl"
postgres_id="$("${compose[@]}" ps -q postgres)"
[[ -n "$postgres_id" ]]
schema_fingerprint() {
  # Только хеш применённых миграций; нет пользовательских данных и SQL восстановления.
  "${compose[@]}" exec -T postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT name,sha256 FROM schema_migrations ORDER BY name"' | sha256sum
}
schema_before="$(schema_fingerprint)"
printf '%s\n' "$schema_before" > "$state/schema-before.sha256"
running=()
declare -A old_images old_tags
printf 'services:\n' > "$state/rollback-images.yml"
while IFS= read -r service; do
  case "$service" in worker|composer|bot|admin|max-web|maxbot|vkbot|illustrator|speaker) ;;
    *) continue;;
  esac
  container="$("${compose[@]}" ps -q "$service")"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
  old_images["$service"]="$(docker inspect --format '{{.Image}}' "$container")"
  old_tags["$service"]="$(docker inspect --format '{{.Config.Image}}' "$container")"
  docker tag "${old_images[$service]}" "bible-messenger-bot:rollback-${previous:0:12}-$service"
  printf '  %s:\n    image: %s\n' "$service" "${old_images[$service]}" >> "$state/rollback-images.yml"
  running+=("$service")
done < <("${compose[@]}" ps --services --filter status=running)
(( ${#running[@]} > 0 ))
changed=false
rollback() {
  code=$?
  trap - EXIT
  if (( code != 0 )) && [[ "$changed" == true ]]; then
    if current_schema="$(schema_fingerprint)" && [[ "$current_schema" == "$schema_before" ]]; then
      echo 'Deployment failed; restoring previous code and images (schema unchanged).'
      git switch --detach "$previous" || code=1
      for service in "${running[@]}"; do docker tag "${old_images[$service]}" "${old_tags[$service]}" || code=1; done
      rollback_compose=(docker compose --project-directory "$project" -f "$state/previous-compose.yml" -f "$state/rollback-images.yml" --profile '*')
      for service in "${running[@]}"; do
        "${rollback_compose[@]}" up -d --no-deps --wait --wait-timeout 180 "$service" || code=1
      done
    else
      echo 'Schema changed or cannot be checked: stop application services; operator intervention required. Database is NOT restored automatically.' >&2
      "${compose[@]}" stop "${running[@]}" || code=1
    fi
  fi
  printf '%s\n' "$code" > "$state/exit-code"
  exit "$code"
}
trap rollback EXIT
echo 'Creating and verifying the cloud database backup before deployment.'
BACKUP_LOCAL_ONLY=0 bash backup.sh
changed=true
git switch main
git merge --ff-only "$target"
bash deploy/rolling-update.sh --yes
sha256sum --check "$state/environment.sha256"
[[ "$("${compose[@]}" ps -q postgres)" == "$postgres_id" ]]
for service in "${running[@]}"; do
  container="$("${compose[@]}" ps -q "$service")"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
done
"${compose[@]}" exec -T "${running[0]}" python -m app.cli migrate --check
if [[ " ${running[*]} " == *' admin '* ]]; then
  address="$("${compose[@]}" port admin 8080)"
  curl --fail --silent --show-error "http://$address/ready"
fi
"${compose[@]}" ps --format json > "$state/services-after.jsonl"
printf '%s\n' "$target" > "$state/deployed-commit"
printf '%s\n' "$target" > runtime-evidence/auto-deploy/deployed-commit
echo "DEPLOYED: $target; application services healthy; PostgreSQL and configuration preserved."
