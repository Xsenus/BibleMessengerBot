.PHONY: test lint check up down logs import stats backup

test:
	pytest

lint:
	ruff format --check .
	ruff check .

check: test lint
	python -m compileall -q app tests
	bash -n install.sh update.sh backup.sh restore.sh diagnose.sh

up:
	docker compose up -d postgres
	docker compose run --rm bootstrap
	docker compose up -d bot worker admin

down:
	docker compose down

logs:
	docker compose logs -f bot worker admin

import:
	docker compose run --rm bootstrap python -m app.cli import --refresh

stats:
	docker compose run --rm --no-deps bot python -m app.cli stats

backup:
	./backup.sh
