# Архитектура

## Процессы

### bootstrap

Применяет `sql/schema.sql`, добавляет книги, темы и планы, получает каталог,
применяет лицензионную политику и импортирует стихи.

### bot

aiogram 3 в long polling. Обрабатывает команды и сохраняет настройки в
PostgreSQL. Webhook, домен и внешний HTTP-порт не нужны.

### worker

Каждые несколько секунд выбирает просроченные подписки через
`FOR UPDATE SKIP LOCKED`, ставит lock token, создаёт идемпотентную запись
`delivery_log`, отправляет сообщения и рассчитывает следующий запуск с IANA
timezone.

### admin

FastAPI: `/health`, `/ready`, защищённая Basic Auth панель и API с
`X-Admin-Key`.

### postgres

Единый источник состояния. Отдельный Redis не требуется.

## Поток импорта

```text
translations.csv + licences.tsv
        -> license policy
        -> profile selector
        -> corpus file + vref.txt
        -> line validation + SHA-256
        -> PostgreSQL transaction
```

## Масштабирование

Можно запустить несколько worker: `SKIP LOCKED` и `lock_token` не позволяют им
одновременно взять одну подписку. Общий Telegram rate limit в текущей версии
локален каждому worker, поэтому при горизонтальном масштабировании следует
делить допустимый лимит между экземплярами либо добавить распределённый
лимитер.
