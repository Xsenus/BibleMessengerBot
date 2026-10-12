#!/usr/bin/env bash
# Обновление без долгой остановки: сначала проверка миграций, затем поочерёдный перезапуск служб.
# Запускать из каталога нового релиза (с .env). Использование: bash deploy/rolling-update.sh [--yes]
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
[[ -f .env ]] || { echo 'Скопируйте рабочий .env в каталог релиза.' >&2; exit 2; }

echo '1/4 Сборка образа…'
docker compose build

echo '2/4 Проверка миграций (ничего не меняет)…'
set +e
docker compose run --rm --no-deps bootstrap python -m app.cli migrate --check
code=$?
set -e
case $code in
  0) echo 'Новых миграций нет.' ;;
  3) echo 'Есть ожидающие миграции: они применяются эксклюзивно, службы будут остановлены на время миграции.'
     [[ "${1:-}" == --yes ]] || { read -r -p 'Продолжить? [y/N] ' a; [[ "$a" == y ]] || exit 1; }
     docker compose stop bot worker composer admin maxbot vkbot illustrator speaker 2>/dev/null || true
     docker compose run --rm --no-deps bootstrap python -m app.cli migrate ;;
  *) echo "Проверка миграций не прошла (код $code) — обновление остановлено." >&2; exit "$code" ;;
esac

echo '3/4 Перезапуск служб по одной (очередь доставки сохраняется в базе)…'
for service in worker composer bot admin maxbot vkbot illustrator speaker; do
  if docker compose config --services | grep -qx "$service"; then
    docker compose up -d --no-deps "$service" || true
    sleep 5
  fi
done

echo '4/4 Проверка состояния…'
docker compose ps
curl -fsS "http://127.0.0.1:${ADMIN_HOST_PORT:-8080}/ready" && echo
