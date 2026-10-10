# Мониторинг и оповещения

## Метрики Prometheus

`GET /metrics` на административной странице (`127.0.0.1:8080`, HTTP Basic: пользователь `admin`, пароль `ADMIN_API_KEY`).
Значения вычисляются из PostgreSQL при каждом запросе, поэтому одинаковы для всех процессов и переживают перезапуск.

| Метрика | Смысл |
|---|---|
| `bible_deliveries{status}` | доставки по статусам |
| `bible_oldest_outstanding_delivery_seconds` | возраст самой старой неотправленной доставки |
| `bible_deliveries_sent_last_hour`, `bible_deliveries_failed_last_day` | поток и ошибки |
| `bible_service_heartbeat_age_seconds{service}` | время с последнего сигнала службы |
| `bible_chats_active`, `bible_subscriptions_enabled` | размер аудитории |
| `bible_image_budget_spent_usd` / `bible_image_budget_limit_usd` | бюджет картинок за месяц (UTC) |
| `bible_image_jobs{state}`, `bible_image_provider_blocked{provider}` | очередь и блокировки генераторов |
| `bible_backup_age_seconds{kind}` | возраст последней проверенной копии |

Пример `scrape_config` для Prometheus через SSH-туннель или локальный агент:

```yaml
- job_name: bible
  metrics_path: /metrics
  basic_auth: {username: admin, password_file: /etc/prometheus/bible-admin-key}
  static_configs: [{targets: ['127.0.0.1:8080']}]
```

## Оповещения владельцу в Telegram

`python -m app.alerts` проверяет те же данные и пишет владельцу бота (`is_owner`) при проблеме:
служба молчит > 3 мин, неотправленная доставка > 30 мин, доставки с неопределённым исходом,
бюджет картинок ≥ 80 %, заблокированный провайдер, копия старше 36 ч. Повтор — раз в `ALERT_REPEAT_HOURS` (6 по умолчанию),
при исчезновении проблемы приходит сообщение «Восстановлено». Состояние хранится в `app_settings`.

Запуск по таймеру systemd:

```bash
sudo cp deploy/bible-messenger-alerts.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now bible-messenger-alerts.timer
```

Разовая проверка: `docker compose --profile maintenance run --rm --no-deps alerts`.
