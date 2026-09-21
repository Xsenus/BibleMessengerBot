# Эксплуатация

## Состояние

```bash
docker compose ps
./diagnose.sh
curl http://127.0.0.1:8080/ready
```

## Логи

```bash
docker compose logs -f bot worker admin
docker compose logs --tail=200 bootstrap
```

## Повторный импорт и расширение языков

```bash
sed -i 's/^BIBLE_PROFILE=.*/BIBLE_PROFILE=extended/' .env
docker compose run --rm bootstrap python -m app.cli import --refresh
docker compose restart bot worker admin
```

Предварительный список без импорта:

```bash
docker compose run --rm bootstrap python -m app.cli preview --profile extended --refresh
```

## Резервное копирование

```bash
./backup.sh
```

Создаются `.sql.gz` и `.sha256` с правами `600`.

Восстановление:

```bash
./restore.sh backups/biblebot-YYYYMMDDTHHMMSSZ.sql.gz
```

## Обновление приложения

Замените файлы новой версией, сохранив `.env`, затем:

```bash
./update.sh
```

## Остановка

```bash
docker compose stop
```

Полное удаление контейнеров без удаления данных:

```bash
docker compose down
```

Удаление вместе с БД необратимо:

```bash
docker compose down -v
```
