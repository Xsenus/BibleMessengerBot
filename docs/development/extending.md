# Как расширять систему

## Добавить генератор картинок

**Без кода:** если сервис совместим с `POST {адрес}/images/generations` — добавьте его в панели («Генераторы картинок» → шаблон «Другой сервис»).

**С кодом** (сервис с другим протоколом): в `app/services/free_image_providers.py`

1. добавьте функцию-адаптер `async def _имя(spec, prompt, client) -> bytes` (вернуть байты картинки или бросить `GenerationError` с кодом);
2. внесите её в словарь `_RUNNERS` и имя в `ADAPTERS`, укажите, нужен ли ключ (`NEEDS_KEY`);
3. добавьте имя адаптера в проверку `CHECK` миграции (`image_custom_providers.adapter`) — новой миграцией;
4. добавьте шаблон в `TEMPLATES` (подпись, пошаговая инструкция `howto`);
5. напишите тест по образцу `tests/test_free_image_providers.py` (ответы имитируются `httpx.MockTransport`).

Выбор провайдера, лимиты, паузы после ошибок, учёт попыток и защита от утечки текста стиха уже реализованы в `image_router.py` и действуют автоматически.

## Добавить источник видео

В `app/services/video_watch.py`: функция разбора ответа (`parse_<сервис>`), ветка в `fetch()`, разбор ID/ссылки канала в `parse_handle()`, значение в `PLATFORMS` и в `CHECK` миграции `video_feeds.platform`. Хосты нового сервиса добавьте в `app/net/services.py`, чтобы работали прокси.

## Добавить способ получения постов (источник контента)

Передайте событие в `chat_sources.ingest_post(connection, platform, external_id, message_id, text, meta, ...)` из обработчика своей платформы — режимы, дедупликация, защита адресов и публикация работают сами.

## Добавить сервис в маршруты (прокси)

Строка в `SERVICES` и окончания хостов в `HOSTS` (`app/net/services.py`); клиент создавайте через `app.net.transport.routed_client()` или `SmartTransport`.

## Добавить раздел в панель

1. Маршруты — в `app/web/api*.py` (авторизация подключается зависимостью роутера).
2. Страница — `admin-ui/src/pages/<Имя>.jsx` и строка в `PAGES` (`App.jsx`); справку оформляйте компонентом `Help`.
3. Тест по образцу `tests/test_admin_system_api_postgres.py`; сборка — `npm run build` в `admin-ui/`. Для панели — мок ответа в `admin-ui/tests/fixtures.js`, раздел в `admin-ui/tests/sections.js` и, если есть формы, проверка запроса в `admin-ui/tests/forms.spec.js`; запуск — `npm test` в `admin-ui/`. Новый раздел из `sections.js` автоматически попадает в проверку доступности (`tests/a11y.spec.js`, axe-core): контраст, заголовки, подписи полей и т. п.

## Добавить новый вид рассылки

Используйте `fanout.enqueue(connection, key=..., text=..., mode=..., audience=...)`: ключ должен быть стабильным и уникальным для рассылки (повтор безопасен). Режим добавьте в `IMMUTABLE_MODES` (`app/worker/delivery.py`), если задания не должны отменяться при смене настроек чата.
