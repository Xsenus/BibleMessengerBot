# База данных

## Основные таблицы

- `languages` — ISO-код, название, письмо и направление;
- `translations` — редакции, лицензии, источник, SHA-256 и статистика;
- `books` — канонический порядок книг;
- `book_names` — локализованные названия;
- `verses` — перевод, книга, глава, стих и текст;
- `telegram_users` — пользователи и личные настройки;
- `telegram_chats` — личные чаты, группы и каналы;
- `subscriptions` — режимы и расписания;
- `reading_progress` — личный прогресс;
- `topics`, `topic_references` — тематические подборки;
- `delivery_log` — идемпотентный журнал отправок;
- `import_runs`, `import_failures` — аудит загрузки данных.

## Целостность

Уникальный ключ стиха:

```text
translation_id + book_code + chapter + verse
```

Импорт одной редакции выполняется в транзакции. Перед заменой новая версия
полностью скачивается, сопоставляется с `vref.txt` и проверяется. Ошибка одной
редакции фиксируется в `import_failures`, не откатывая успешно загруженные
языки.

## Поиск

Для текста используется нормализованная колонка `search_text` и GIN trigram
индекс `pg_trgm`. Это работает для разных письменностей без выбора одного
словаря PostgreSQL.

## Объём

Фактический размер зависит от профиля и числа редакций. Посмотреть:

```bash
docker compose exec -T postgres psql -U biblebot -d biblebot -c \
  "select pg_size_pretty(pg_database_size('biblebot'));"
```

## Прямой отчёт

```bash
docker compose run --rm --no-deps bot python -m app.cli stats
```
