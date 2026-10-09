# MAX: HTTPS и продление IP-сертификата

Webhook MAX требует публичный HTTPS на 443 и доверенную цепочку с совпадающим
именем/SAN. HTTP и самоподписанный сертификат не подходят. Порт `MAX_HOST_PORT`
в Compose локальный: изменение его значения не меняет требования MAX к 443.
[Требования MAX](https://dev.max.ru/docs-api/methods/POST/subscriptions).

## Состояние на 9 октября 2026

Для VPS получен доверенный IP-сертификат Let's Encrypt с профилем `shortlived`.
HTTPS, отказ Webhook без секретного заголовка и настоящий VLESS REALITY-сеанс
проверены через временный шлюз на отдельных локальных портах. Публичный маршрут
443 не менялся: существующий Xray продолжает обслуживать VPN напрямую.

Скрипт `max-cert-renew.sh --dry-run` успешно выполнил ACME-проверку через
тестовый центр Let's Encrypt. Он продлевает только `bible-max-ip`, не весь
набор сертификатов и не тестовую lineage. Рабочий сертификат при `--dry-run`
не заменяется; перечитывание TLS также не выполняется.

На VPS установлены service и timer: штатный запуск завершился с `Result=success`,
`ExecMainStatus=0`; таймер включён. Контейнер `bible-max-acme` healthy, внешняя
HTTP-проверка `/max-edge-health` вернула 200, а `/max/webhook` на HTTP — 404.
Отдельный тест подтвердил отказ при занятом порту без остановки слушателя и
успешный запуск после его закрытия, даже при оставшихся соединениях `TIME_WAIT`.

При использовании общего шлюза именованный SNI VPN передаётся в Xray,
пустой SNI или SNI IP-адреса — в HTTPS MAX. Xray будет видеть IP шлюза.
Владелец разрешил включение общего шлюза 9 октября 2026. Подготовлены
`scripts/max_https_edge.py` и units `bible-max-edge`/`bible-max-edge-guard`.
Регистрация IP URL в MAX и живая доставка событий проверяются при активации.

## Общий порт с существующим Xray

Шлюз слушает 29443, HTTPS-приёмник — только `127.0.0.1:28443`; Xray остаётся на 443.
Одна помеченная iptables REDIRECT-запись направляет новые входящие соединения на 443
через шлюз, только на заданном публичном интерфейсе/IP. OUTPUT не меняется, поэтому
локальный backend Xray не зацикливается. NAT применяется к первому пакету потока:
уже установленные прямые VPN-сеансы сохраняются. Глобальные таблицы не очищаются
и не сохраняются поверх правил других служб.

```sh
cd /opt/bible-messenger-bot
systemd-analyze verify deploy/bible-max-edge.service deploy/bible-max-edge-guard.service deploy/bible-max-edge-guard.timer
install -m 0644 deploy/bible-max-edge.service deploy/bible-max-edge-guard.service deploy/bible-max-edge-guard.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bible-max-edge.service bible-max-edge-guard.timer
python3 scripts/max_https_edge.py status
```

Для другого сервера изменить `MAX_EDGE_IP` и `MAX_EDGE_INTERFACE` в обоих units.
Чужой контейнер `bible-max-edge` не переиспользуется. Имя/метку оставить для
совместимости со скриптом продления сертификата.

Guard раз в 20 секунд проверяет доверенный TLS через frontend, MAX `/health`
и доступность исходного Xray. При неисправности удаляет только свои точные
помеченные правила: новые VPN-соединения идут напрямую в Xray. Если сервис
шлюза остаётся активным и проверка проходит, маршрут восстанавливается.
Соединения неисправного прокси могут оборваться: VPN-клиент должен переподключиться.

```sh
systemctl disable --now bible-max-edge-guard.timer
systemctl disable --now bible-max-edge.service
```

Остановка удаляет только маршрут проекта и останавливает его nginx; Xray продолжает
работу. После включения проверить внешний HTTPS, настоящую авторизованную VPN-сессию,
автоматический fallback/возврат и действительную доставку событий из MAX.

## Подготовка и обслуживание

Первичный сертификат нужно получить Certbot 5.4+ с `--preferred-profile shortlived`,
`--ip-address <публичный-IP>`, `--cert-name bible-max-ip` и HTTP-01 `--webroot`.
Webroot внутри Certbot — `/var/www/certbot`. Инструкция центра:
[IP-сертификаты Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html).

Данные принадлежат этому проекту и находятся в `certs/max-edge`:

| Каталог | Назначение внутри контейнера |
| --- | --- |
| `webroot` | `/var/www/certbot`, только публичные ACME challenge-файлы |
| `letsencrypt` | `/etc/letsencrypt`, приватный ключ, цепочки, ACME-аккаунт и renewal-конфигурация |
| `acme-work` | `/var/lib/letsencrypt` |
| `acme-logs` | `/var/log/letsencrypt` |

Родительский каталог, `letsencrypt`, `acme-work` и `acme-logs` должны иметь права
0700, `webroot` — 0755. `certs` исключён из Git и Docker-образа. В HTTP-контейнер
монтируются только конфигурация и `webroot` для чтения; ключи ему не доступны.

Скрипт запускает собственный `bible-max-acme` на свободном порту 80. В нём открыты
только challenge-файлы и `/max-edge-health`; остальные пути возвращают 404.
Если порт занят, другая служба не останавливается. Контейнер с совпадающим именем
используется только при наличии метки `io.bible-messenger.role=max-acme`.
Образы nginx и Certbot закреплены по digest в скрипте, автоматического перехода
на новые версии нет. `MAX_CERT_PROJECT_DIR` позволяет выбрать отдельный каталог
для проверки; в обычной эксплуатации используется каталог самого скрипта.

Перед включением таймера на VPS:

```sh
cd /opt/bible-messenger-bot
bash -n max-cert-renew.sh
bash max-cert-renew.sh --dry-run
systemd-analyze verify deploy/bible-max-cert-renew.service deploy/bible-max-cert-renew.timer
install -m 0644 deploy/bible-max-cert-renew.service /etc/systemd/system/bible-max-cert-renew.service
install -m 0644 deploy/bible-max-cert-renew.timer /etc/systemd/system/bible-max-cert-renew.timer
systemctl daemon-reload
systemctl enable --now bible-max-cert-renew.timer
systemctl start bible-max-cert-renew.service
systemctl list-timers bible-max-cert-renew.timer
journalctl -u bible-max-cert-renew.service --no-pager -n 50
```

Срок IP-сертификата — около шести дней. Таймер проверяет необходимость продления
каждые шесть часов UTC, со случайной задержкой до 15 минут и запуском пропущенной
проверки после перезагрузки. Параллельные запуски исключены `flock`.
Неудачный ACME-запрос оставляет прежний сертификат; служба завершится с ошибкой.
При успешной обычной проверке проверяется запас срока не меньше суток.

Когда публичный HTTPS-шлюз будет активирован, назвать его `bible-max-edge` и
добавить метку `io.bible-messenger.role=max-edge`. Монтировать всю директорию
`letsencrypt` только для чтения, чтобы ссылки `live` после обновления указывали
на новые файлы из `archive`. После каждой успешной обычной проверки скрипт
выполняет `nginx -t` и `nginx -s reload` в этом контейнере. Повтор после ошибки
перечитывания не зависит от того, требуется ли ещё одно продление сертификата.
Xray, его конфигурация и firewall скриптом не меняются.

Для отключения обслуживания сертификата:

```sh
systemctl disable --now bible-max-cert-renew.timer
```

Перед остановкой HTTP-контейнера убедиться, что продление больше не требуется.
Не удалять ключи и ACME-аккаунт ради обычной остановки. Этот таймер обслуживает
сертификат, а не включает публичную MAX-подписку или приложение `maxbot`.
