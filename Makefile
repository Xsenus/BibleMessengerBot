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
