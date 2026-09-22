# База данных

Используется PostgreSQL 16 в отдельном постоянном Docker-томе. `DATABASE_URL` связывает приложение с БД; внутри Compose хост — `postgres`. Структура создаётся программой, отдельно скачивать сторонний SQL-дамп не требуется.

## Миграции

`app.db.apply_schema` применяет `sql/schema.sql`, затем SQL-файлы из `sql/migrations/` по имени. Таблица `schema_migrations` хранит имя, SHA-256 и время применения. Повторный запуск пропускает уже применённое; изменённый ранее применённый файл вызывает отказ. Миграции сериализованы advisory lock, каждый файл применяется в транзакции.

Не редактируйте применённый SQL и не удаляйте записи `schema_migrations` для обхода проверки. Для обновления добавляется следующая миграция. Перед изменением схемы используйте backup и остановку приложения.

## Основные таблицы

| Область | Таблицы и назначение |
|---|---|
| Корпус | `languages`, `translations`, `books`, `book_names`, `verses`, `translation_chapters` — языки, издания, координаты стихов и индексы чтения. |
| Происхождение | `translation_sources`, `translation_book_names`, `import_runs`, `import_failures` — источники, alias, права, хеши, родные названия книг и история импорта. |
| Telegram | `telegram_users`, `telegram_chats` — пользователи и настройки назначений. |
| Чтение | `subscriptions`, `reading_progress`, `chat_reading_progress`, `reading_plans`, `reading_plan_items` — расписания, прогресс и планы. |
| Темы | `topics`, `topic_references` — редакционные подборки и проверяемые ссылки. |
| Доставка | `delivery_log`, `operator_events`, `service_heartbeats` — очередь, решения оператора и состояние процессов. |
| Служебное | `app_settings`, `schema_migrations` и таблицы ограничений/координации — настройки и контроль исполнения. |

`verses` сохраняет книгу, главу, стих, текст, `verse_end`, признаки продолжения диапазона, исходную строку и плотный `ordinal` для чтения. `translation_chapters` содержит последовательный индекс глав. Пустые продолжения диапазона не выдаются за отдельный видимый стих.

Издание имеет источник и `source_translation_id`. Полный внешний идентификатор `source:id` разрешается также через `translation_sources`; неоднозначный короткий ID не выбирается случайно. `source_sha256` описывает исходный файл, `content_sha256` — нормализованные координаты и содержимое, `numbering_system` — пространство нумерации. Совпадение названий или числа строк не доказывает одинаковый текст.

## Запись и аудит

Одна транзакция импорта проверяет права, записывает метаданные и порции строк, строит индекс глав, сравнивает хеш фактических строк PostgreSQL и сохраняет provenance. Ошибка откатывает это издание. Завершённые транзакции других изданий остаются.

Полный аудит сверяет физическое число строк/книг/глав, видимые стихи, плотность ordinal и индекса глав, диапазоны и содержательный хеш новых записей. Структурный флаг `canonical_66_complete` не доказывает текстологическую точность и не равен полному православному/католическому составу.

```bash
docker compose run --rm --no-deps bootstrap python -m app.cli stats
docker compose run --rm --no-deps bootstrap python -m app.cli audit
```

Для прямого чтения SQL на собственном сервере:

```bash
docker compose exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Пример запроса без персональных данных:

```sql
SELECT l.code, t.source_name, t.source_translation_id,
       t.book_count, t.verse_count, t.coverage, t.audit_status
FROM translations AS t
JOIN languages AS l ON l.id = t.language_id
WHERE t.is_active
ORDER BY l.code, t.source_translation_id;
```

Пользовательские настройки не следует менять произвольным SQL: обход блокировок и согласования очереди может нарушить доставку. Используйте команды Telegram и штатные сервисы. Резервирование/восстановление — [OPERATIONS.md](OPERATIONS.md), сведения о приватности — [SECURITY.md](SECURITY.md).
