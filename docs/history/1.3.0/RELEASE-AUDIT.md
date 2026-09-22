# Проверка BibleMessengerBot 1.3.0

**Статус: OFFLINE_PASS_LIVE_UNVERIFIED**. Это отчёт офлайн-проверок, а не приёмка production.

Выполнено тестов успешно: **317**. Ошибок: **0**. Пропущено единиц сбора/тестов: **2**.
Пропуски перечислены ниже и в XML/JSON; они не посчитаны как прошедшие. tests.test_aiogram_contracts; tests.test_postgres_integration

## Выполненные проверки

- PASSED — COMPILE
- PASSED — TESTS
- PASSED — SHELL-backup
- PASSED — SHELL-diagnose
- PASSED — SHELL-fill_database
- PASSED — SHELL-install
- PASSED — SHELL-restore
- PASSED — SHELL-update
- PASSED — SHELL-verify
- PASSED — JSON_YAML_PARSE_ONLY
- PASSED — BASIC_SECRET_AND_PATH_SCAN
- PASSED — REQUIRED_FILES

Полные команды, причины пропусков и результаты: RELEASE-AUDIT.json, evidence/TESTS.txt и TESTS.xml. Разбор YAML не является запуском Docker Compose. Компиляция Python не проверяет отсутствующие зависимости или SQL на сервере.

## Не выполнено

- Actual PostgreSQL/asyncpg integration: missing asyncpg or RUN_DB_TESTS is not enabled.
- Actual aiogram model/authorization integration suite: missing library.
- Docker image build and docker compose config/runtime on target VPS.
- Full authentic Bible download/import; no complete edition is physically bundled.
- Real Telegram Bot API requests, channel/group/private delivery and scheduled live acceptance.
- Real backup/restore, sustained load/soak tests, independent textual/native-language review.
- Ruff, dependency vulnerability audit and fully hash-locked reproducible image build.

## Фактическое наполнение

Каталогов UI: 24. Источников: 3. Профиль all-open не ограничен списком из 56 языков, но фильтрует лицензии и формат. Полностью скачанных изданий в ZIP: **0**. Готового дампа нет.

Установщик должен выполнить реальные интеграционные тесты, импорт и аудит на VPS. До их успешного завершения публикации не запускаются. Это дополнительная проверка на сервере, а не уже выполненная здесь работа. Гарантии отсутствия всех ошибок нет.
