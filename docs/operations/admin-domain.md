# Домен админки

Рабочая панель: **https://bible.blagodaty.ru/panel/**, A-запись `82.38.69.203`. Backend остаётся на `127.0.0.1:8080`. HTTPS-домен маршрутизируется существующим `bible-max-edge` по SNI; запросы MAX по IP и остальные SNI для Xray сохраняют прежние маршруты. Открывать порт 8080 наружу не требуется.

Вход: HTTP Basic с пользователем `admin` и текущим `ADMIN_API_KEY`, затем тот же ключ в панели. Nginx хранит только SHA-512 crypt хеш в приватном `certs/max-edge/letsencrypt/admin-auth/panel.htpasswd`. При смене `ADMIN_API_KEY` обновите и этот хеш. Права файла `0640 root:101`, каталога `0750 root:101`; корень монтируемого `letsencrypt` допускает только проход (`0711`), приватные ключи сертификатов остаются `0600`.

## Состав конфигурации

- `deploy/max-edge.nginx.conf.template` подключает необязательные `admin-stream/*.conf` внутри SNI map и `admin-http/*.conf` внутри HTTP. Пустой glob не включает админку на других установках.
- `certs/max-edge/letsencrypt/admin-stream/panel.conf` содержит `bible.blagodaty.ru 127.0.0.1:28444;`.
- `admin-http/panel.conf` создаётся из `deploy/admin-domain.nginx.conf.template` заменой `@ADMIN_DOMAIN@`. Доступны только `/panel/` и `/api/admin/`; `/` ведёт на панель. Остальные пути, включая приватную диагностику, не проксируются.
- Certbot `certonly --webroot` использует существующий HTTP-01 webroot `certs/max-edge/webroot`, имя сертификата `bible-admin-domain` и домен `bible.blagodaty.ru`. VPN и приёмник MAX при выпуске не останавливаются.
- `max-cert-renew.sh` поддерживает IP-сертификат `bible-max-ip` и, при наличии renewal-конфигурации, сертификат `bible-admin-domain`. Существующий systemd timer продлевает оба и проверяет конфигурацию до reload nginx. Ключи, аккаунт ACME и пароли хранятся только в игнорируемом `certs/`.

При первичной установке сначала выпустите сертификат и подготовьте auth-файл, затем сохраните приватные include-файлы. Сохраните прежний `edge.conf`, обновите его содержимое из актуального шаблона с IP (bind mount требует сохранения inode), выполните `docker exec bible-max-edge nginx -t` и только затем `nginx -s reload`. При ошибке проверки восстановите прежнее содержимое; не перезапускайте VPN и не меняйте iptables. После установки `scripts/max_https_edge.py start` сверяет актуальный шаблон и сохраняет доменные include-файлы.

Проверка: без авторизации домен возвращает 401; с Basic открывается панель, но API без `X-Admin-Key` продолжает возвращать 401. После двух проверок авторизации список и POST скачивания доступны; для подготовки большого dump тайм-аут прокси 600 секунд и отключена буферизация ответа. См. [документацию nginx](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) и [Certbot webroot](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot).
