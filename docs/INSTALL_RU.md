# Установка на VPS

## Требования

- Ubuntu 22.04/24.04 или актуальный Debian;
- минимум 2 vCPU, 2 ГБ RAM и 15 ГБ SSD для `core`;
- для `extended` желательно 4 ГБ RAM и 30 ГБ SSD;
- исходящий HTTPS к Telegram, GitHub и eBible;
- токен Telegram-бота от `@BotFather`.

## Автоматическая установка

```bash
sudo apt update
sudo apt install -y unzip
unzip BibleMessengerBot-1.1.0.zip
cd BibleMessengerBot-1.1.0
sudo bash install.sh
```

Будет запрошен только `BOT_TOKEN`. Остальные пароли генерируются автоматически.

Можно передать токен без интерактивного ввода:

```bash
sudo BOT_TOKEN='123456:ABC...' BIBLE_PROFILE=extended bash install.sh
```

## Что делает установщик

1. Устанавливает Docker Engine и Compose plugin, если их нет.
2. Создаёт `.env` с правами `600`.
3. Генерирует пароль PostgreSQL, ключ панели и одноразовый код владельца.
4. Собирает контейнер приложения.
5. Запускает PostgreSQL.
6. Создаёт таблицы и служебные данные.
7. Загружает актуальный каталог переводов.
8. Отбирает редакции согласно профилю и лицензии.
9. Скачивает тексты, проверяет строки и SHA-256, импортирует их транзакционно.
10. Запускает bot, worker и admin.

## Первый вход

Отправьте боту строку, показанную в конце установки:

```text
/claim КОД
```

Код не отправляется автоматически и не публикуется в логах бота.

## Панель

По умолчанию панель слушает только loopback VPS:

```text
http://127.0.0.1:8080/admin
```

Откройте SSH-туннель с компьютера:

```bash
ssh -L 8080:127.0.0.1:8080 USER@SERVER_IP
```

Затем откройте `http://127.0.0.1:8080/admin`. Логин `admin`, пароль указан
установщиком и сохранён как `ADMIN_API_KEY` в `.env`.

## Выбор профиля

```bash
sudo BIBLE_PROFILE=core bash install.sh
```

Загрузить до трёх разрешённых редакций на каждый язык профиля:

```bash
sudo BIBLE_PROFILE=extended MAX_EDITIONS_PER_LANGUAGE=3 bash install.sh
```

Изменить профиль после установки:

```bash
sed -i 's/^BIBLE_PROFILE=.*/BIBLE_PROFILE=extended/' .env
sudo docker compose run --rm bootstrap
sudo docker compose restart bot worker admin
```

## Проверка

```bash
docker compose ps
./diagnose.sh
docker compose logs -f bot worker
```

Готовность базы:

```bash
curl http://127.0.0.1:8080/ready
```
