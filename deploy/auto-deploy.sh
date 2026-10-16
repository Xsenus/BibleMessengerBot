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
previous="$(git rev-parse HEAD)"
compose=(docker compose --profile '*')
state="$project/runtime-evidence/auto-deploy/$(date -u +%Y%m%dT%H%M%SZ)-${target:0:12}-$$"
mkdir -p "$state"
exec > >(tee -a "$state/deploy.log") 2>&1
echo "Deploy $previous -> $target"
running=()
declare -A old_images old_tags
changed=false
stage=checking
failure_code=preflight_failed
failure_service=''
failed_stage=''
failure_exit=''
outcome_override=''
publish_status() {
  # В админку передаётся отдельная квитанция, а не приватные файлы runtime-evidence.
  local reason='' failed='' code='' service=''
  if [[ "$1" != running && "$1" != succeeded ]]; then
    reason="$failure_code"; failed="$failed_stage"; code="$failure_exit"; service="$failure_service"
  fi
  python3 "${BIBLE_DEPLOY_STATUS_WRITER:-/usr/local/libexec/bible-deploy-status}" \
    "$project/runtime-evidence/deploy-status" "$state" "$target" "$previous" "$1" "$2" \
    "$reason" "$failed" "$code" "$service"
}
schema_fingerprint() {
  # Только хеш применённых миграций; нет пользовательских данных и SQL восстановления.
  "${compose[@]}" exec -T postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT name,sha256 FROM schema_migrations ORDER BY name"' | sha256sum
}
rollback() {
  code=$?
  trap - EXIT
  failed_stage="$stage"
  failure_exit="$code"
  outcome=failed
  if (( code != 0 )) && [[ "$changed" == true ]]; then
    if current_schema="$(schema_fingerprint)" && [[ "$current_schema" == "$schema_before" ]]; then
      echo 'Deployment failed; restoring previous code and images (schema unchanged).'
      publish_status running rollback || true
      restored=true
      git switch --detach "$previous" || restored=false
      for service in "${running[@]}"; do docker tag "${old_images[$service]}" "${old_tags[$service]}" || restored=false; done
      rollback_compose=(docker compose --project-directory "$project" -f "$state/previous-compose.yml" -f "$state/rollback-images.yml" --profile '*')
      for service in "${running[@]}"; do
        "${rollback_compose[@]}" up -d --no-deps --wait --wait-timeout 180 "$service" || restored=false
      done
      stage=rollback
      [[ "$restored" == false ]] || outcome=rolled_back
    else
      echo 'Schema changed or cannot be checked: stop application services; operator intervention required. Database is NOT restored automatically.' >&2
      "${compose[@]}" stop "${running[@]}" || code=1
      outcome=manual_intervention
      stage=stopped
    fi
  fi
  if (( code == 0 )); then outcome=succeeded; stage=completed; fi
  if [[ -n "$outcome_override" ]]; then outcome="$outcome_override"; stage=skipped; fi
  publish_status "$outcome" "$stage" || code=1
  if (( code == 0 )) && [[ "$outcome" == succeeded ]]; then
    printf '%s\n' "$target" > "$state/deployed-commit"
    printf '%s\n' "$target" > runtime-evidence/auto-deploy/deployed-commit
    echo "DEPLOYED: $target; application services healthy; PostgreSQL and configuration preserved."
  fi
  printf '%s\n' "$code" > "$state/exit-code"
  # Уведомление не зависит от БД/бота и не меняет результат установки при отказе Telegram.
  python3 "${BIBLE_DEPLOY_NOTIFIER:-/usr/local/libexec/bible-deploy-notify}" "$project" "$state" \
    || echo 'BIBLE_DEPLOY_NOTICE=unavailable'
  exit "$code"
}
trap rollback EXIT
trap 'failure_code=interrupted; exit 143' TERM
trap 'failure_code=interrupted; exit 130' INT
publish_status running checking
failure_code=configuration_missing
[[ -f .env ]] || { echo 'Missing configuration.' >&2; exit 1; }
failure_code=dirty_checkout
[[ -z "$(git status --porcelain)" ]] || { echo 'Dirty checkout.' >&2; exit 1; }
failure_code=unexpected_branch
branch="$(git branch --show-current)"
[[ -z "$branch" || "$branch" == main ]] || { echo 'Unexpected working branch.' >&2; exit 1; }
failure_code=fetch_failed
git fetch origin main
if [[ "$(git rev-parse origin/main)" != "$target" ]]; then
  failure_code=outdated_commit
  outcome_override=skipped
  echo 'SKIPPED: checked commit is no longer the head of main.'
  exit 0
fi
failure_code=ancestry_failed
git merge-base --is-ancestor "$previous" "$target"
failure_code=compose_invalid
"${compose[@]}" config --quiet
failure_code=disk_space
free_kib="$(df -Pk . | awk 'NR==2 {print $4}')"
[[ "$free_kib" =~ ^[0-9]+$ && "$free_kib" -ge 4194304 ]] || { echo 'At least 4 GiB free disk space is required.' >&2; exit 1; }
failure_code=preflight_failed
sha256sum .env > "$state/environment.sha256"
cp docker-compose.yml "$state/previous-compose.yml"
"${compose[@]}" ps --format json > "$state/services-before.jsonl"
postgres_id="$("${compose[@]}" ps -q postgres)"
[[ -n "$postgres_id" ]]
schema_before="$(schema_fingerprint)"
printf '%s\n' "$schema_before" > "$state/schema-before.sha256"
printf 'services:\n' > "$state/rollback-images.yml"
while IFS= read -r service; do
  case "$service" in worker|composer|bot|admin|max-web|maxbot|vkbot|illustrator|speaker) ;;
    *) continue;;
  esac
  container="$("${compose[@]}" ps -q "$service")"
  failure_code=service_unhealthy
  failure_service="$service"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
  failure_code=preflight_failed
  old_images["$service"]="$(docker inspect --format '{{.Image}}' "$container")"
  old_tags["$service"]="$(docker inspect --format '{{.Config.Image}}' "$container")"
  docker tag "${old_images[$service]}" "bible-messenger-bot:rollback-${previous:0:12}-$service"
  printf '  %s:\n    image: %s\n' "$service" "${old_images[$service]}" >> "$state/rollback-images.yml"
  running+=("$service")
done < <("${compose[@]}" ps --services --filter status=running)
(( ${#running[@]} > 0 ))
failure_service=''
echo 'Creating and verifying the cloud database backup before deployment.'
stage=backup
publish_status running "$stage"
failure_code=backup_failed
BACKUP_LOCAL_ONLY=0 bash backup.sh
changed=true
stage=updating
publish_status running "$stage"
failure_code=activation_failed
git switch main
git merge --ff-only "$target"
bash deploy/rolling-update.sh --yes
stage=verifying
publish_status running "$stage"
failure_code=configuration_changed
sha256sum --check "$state/environment.sha256"
failure_code=postgres_changed
[[ "$("${compose[@]}" ps -q postgres)" == "$postgres_id" ]]
for service in "${running[@]}"; do
  failure_service="$service"
  failure_code=service_unhealthy
  container="$("${compose[@]}" ps -q "$service")"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
  failure_code=revision_mismatch
  revision="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container")"
  [[ "$revision" == "$target" ]]
  printf '{"name":"%s","health":"healthy","commit":"%s"}\n' "$service" "$revision" >> "$state/checked-services.jsonl"
done
failure_service=''
failure_code=migration_check_failed
"${compose[@]}" exec -T "${running[0]}" python -m app.cli migrate --check
if [[ " ${running[*]} " == *' admin '* ]]; then
  failure_code=admin_unready
  address="$("${compose[@]}" port admin 8080)"
  curl --fail --silent --show-error "http://$address/ready"
fi
failure_code=verification_failed
"${compose[@]}" ps --format json > "$state/services-after.jsonl"
