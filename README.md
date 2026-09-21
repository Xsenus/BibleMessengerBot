# BibleMessengerBot 1.1.0

Готовый к размещению на VPS Telegram-бот для многоязычного чтения Библии и
автоматических публикаций в личные чаты, группы и каналы.

## Что входит

- импорт переводов из курируемого корпуса BibleNLP/eBible;
- PostgreSQL: языки, переводы, книги, главы, стихи, лицензии и происхождение;
- строгий фильтр лицензий;
- профили `core`, `extended`, `all-open`;
- личное чтение по порядку с сохранением прогресса;
- стих дня, тематический стих и случайный стих;
- поиск по выбранному переводу;
- расписания для личных чатов, групп и каналов;
- очередь отправки на PostgreSQL, блокировки `SKIP LOCKED`, повторы и защита от дублей;
- административная панель и API;
- Docker Compose, автоматический установщик, резервное копирование и диагностика.

## Установка

На чистом Ubuntu/Debian достаточно токена от `@BotFather`:

```bash
unzip BibleMessengerBot-1.1.0.zip
cd BibleMessengerBot-1.1.0
sudo bash install.sh
```

Установщик сам ставит Docker, генерирует секреты, создаёт PostgreSQL, загружает
выбранные переводы в базу и запускает бот, worker и панель. Домен и SSL для
long polling не требуются.

После установки отправьте боту показанную команду:

```text
/claim ОДНОРАЗОВЫЙ_КОД
```

Подробно: [docs/INSTALL_RU.md](docs/INSTALL_RU.md).

## Профили переводов

По умолчанию используется `extended`: 56 целевых языков и до двух наиболее
полных разрешённых редакций на язык. Выбор основан на лицензии, полноте и
явных предпочтениях, а не на богословской оценке качества.

- `core` — 25 основных языков;
- `extended` — 56 языков;
- `all-open` — все редакции, прошедшие лицензионный фильтр;
- `none` — не импортировать тексты автоматически.

Перед установкой профиль можно задать так:

```bash
sudo BIBLE_PROFILE=core bash install.sh
```

Количество редакций для `core`/`extended` можно изменить:

```bash
sudo BIBLE_PROFILE=extended MAX_EDITIONS_PER_LANGUAGE=3 bash install.sh
```

Режим `all-open` способен занять несколько гигабайт и заметно увеличить время
первого запуска.

## Лицензии

По умолчанию разрешены только:

- Public Domain;
- CC0;
- CC BY;
- CC BY-SA.

NC, ND, неизвестные и помеченные как неперераспространяемые редакции
пропускаются. Для каждой загруженной редакции сохраняются источник, лицензия,
правообладатель, URL, дата источника и SHA-256. Подробнее:
[docs/TRANSLATIONS.md](docs/TRANSLATIONS.md).

## Основные команды бота

```text
/start
/today
/next
/random
/topic [код]
/search текст
/translations [код языка]
/translation ID
/subscribe [sequential|verse|topic|plan] [HH:MM] [Timezone]
/channel @channel [режим] [HH:MM] [Timezone]
/pause
/resume
/unsubscribe [режим]
/status
/help
```

Telegram-бот не может первым написать произвольному человеку: пользователь
должен открыть бота и нажать Start. В группе настройки меняет администратор.
Для канала бот должен быть назначен администратором с правом публикации.

## Архитектура

```text
Telegram -> bot (aiogram) ------\
                                 -> PostgreSQL
scheduler worker ---------------/
admin (FastAPI) ----------------/
bootstrap importer -> BibleNLP/eBible -> PostgreSQL
```

Redis не требуется: задания, блокировки, идемпотентность и журнал доставки
реализованы средствами PostgreSQL.

## Проверка

```bash
pytest
python -m compileall -q app tests
bash -n install.sh update.sh backup.sh restore.sh diagnose.sh
python scripts/release_audit.py
```

## Документация

- [Установка](docs/INSTALL_RU.md)
- [Переводы и лицензии](docs/TRANSLATIONS.md)
- [Проверка источника](docs/SOURCE-VERIFICATION.md)
- [Структура БД](docs/DATABASE.md)
- [Telegram: группы и каналы](docs/TELEGRAM_SETUP.md)
- [Эксплуатация](docs/OPERATIONS.md)
- [Безопасность](docs/SECURITY.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Проверенные ограничения](docs/LIMITATIONS.md)
