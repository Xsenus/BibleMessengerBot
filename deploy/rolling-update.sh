#!/usr/bin/env bash
# Проверить и обновить только уже работающие службы; не включать неподключённые платформы.
# Использование: bash deploy/rolling-update.sh [--yes]
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
[[ -f .env ]] || { echo 'Скопируйте рабочий .env в каталог релиза.' >&2; exit 2; }
compose=(docker compose --profile '*')
"${compose[@]}" config --quiet
running="$("${compose[@]}" ps --services --filter status=running)"
previous=()
for service in worker composer bot admin max-web maxbot vkbot illustrator speaker; do
  if grep -qx "$service" <<<"$running"; then previous+=("$service"); fi
done
resume=false
finish() {
  status=$?
  trap - EXIT
  if [[ "$resume" == true && ${#previous[@]} -gt 0 ]]; then
    echo 'Обновление прервано; запускаем прежние контейнеры остановленных служб.' >&2
    "${compose[@]}" start "${previous[@]}" || status=1
  fi
  exit "$status"
}
trap finish EXIT

echo '1/4 Сборка образа…'
# Every app service shares bootstrap's image; parallel exports to the same tag can stall.
"${compose[@]}" build bootstrap
"${compose[@]}" run --rm --no-deps bootstrap python -m app.neural_audio_admin prepare

echo '2/4 Проверка миграций (ничего не меняет)…'
code=0
"${compose[@]}" run --rm --no-deps bootstrap python -m app.cli migrate --check || code=$?
case $code in
  0) echo 'Новых миграций нет.' ;;
  3) echo 'Есть ожидающие миграции; сначала резервная копия, затем остановка работающих служб.'
     [[ "${1:-}" == --yes ]] || { read -r -p 'Продолжить? [y/N] ' answer; [[ "$answer" == y ]] || exit 1; }
     BACKUP_LOCAL_ONLY=1 bash backup.sh
     resume=true
     if (( ${#previous[@]} )); then "${compose[@]}" stop "${previous[@]}"; fi
     "${compose[@]}" run --rm --no-deps bootstrap python -m app.cli migrate ;;
  *) echo "Проверка миграций не прошла (код $code) — обновление остановлено." >&2; exit "$code" ;;
esac

echo '3/4 Перезапуск работающих служб по одной…'
for service in "${previous[@]}"; do
  "${compose[@]}" up -d --no-deps --wait --wait-timeout 120 "$service"
done

echo '4/4 Проверка состояния…'
"${compose[@]}" ps
if grep -qx admin <<<"$running"; then
  admin_address="$("${compose[@]}" port admin 8080)"
  curl -fsS "http://${admin_address}/ready"
fi
resume=false
