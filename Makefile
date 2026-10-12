.PHONY: test verify audit up down logs stats backup

test:
	python -m pytest -o addopts= -q -rs

verify:
	bash verify.sh

audit:
	python scripts/release_audit.py

up:
	sudo bash install.sh

down:
	docker compose down

logs:
	docker compose logs -f bot worker admin

stats:
	docker compose run --rm --no-deps bootstrap python -m app.cli stats

backup:
	sudo bash backup.sh

# --- Разработка ---------------------------------------------------------------------------
.PHONY: dev-db dev-test lint typecheck chat selfcheck migrate-check loadtest panel monitoring rolling-update

dev-db:  ## поднять локальную базу для тестов (docker)
	docker run -d --rm --name biblebot-dev-db -e POSTGRES_PASSWORD=x -e POSTGRES_USER=biblebot_ci -e POSTGRES_DB=biblebot_ci -p 5432:5432 postgres:16-alpine

dev-test:  ## тесты с базой (нужен dev-db)
	RUN_DB_TESTS=1 DATABASE_URL=postgresql://biblebot_ci:x@localhost:5432/biblebot_ci python -m pytest -q

lint:
	ruff check app tests scripts

typecheck:
	mypy app

chat:  ## имитатор чата: make chat ARGS='"/read Ин 3:16" /bookmarks'
	python scripts/chat_sim.py $(ARGS)

selfcheck:  ## живая проверка баз, ключей, токенов и программ
	docker compose run --rm --no-deps bootstrap python -m app.selfcheck

migrate-check:  ## какие миграции ожидают (ничего не меняет)
	docker compose run --rm --no-deps bootstrap python -m app.cli migrate --check

loadtest:  ## замер пакетной рассылки на временной базе
	python scripts/loadtest_fanout.py

panel:  ## собрать веб-панель
	cd admin-ui && npm ci && npm run build

monitoring:  ## Prometheus + Grafana (профиль monitoring)
	docker compose --profile monitoring up -d prometheus grafana

rolling-update:
	bash deploy/rolling-update.sh
