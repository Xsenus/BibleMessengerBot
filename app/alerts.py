"""Оповещения оператора: проверяет сохранённое состояние и пишет владельцу в Telegram, когда что-то требует внимания (раз в несколько часов, с сообщением о восстановлении)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.db import close_pool, create_pool, wait_for_database
from app.services.metrics import collect

LOGGER = logging.getLogger(__name__)
STATE_KEY = 'alerts_state'


def evaluate(samples: dict[str, float], *, budget_warn: float = 0.8, stale_delivery: float = 1800,
             heartbeat_stale: float = 180, backup_stale: float = 36 * 3600) -> dict[str, str]:
    """Чистая функция: по словарю значений метрик (ключ `имя` или `имя|метка`) вернуть словарь активных оповещений {идентификатор: сообщение}. Проверяются молчащие службы, зависшие и неопределённые доставки, бюджет картинок, блокировки провайдеров и возраст резервных копий."""
    alerts: dict[str, str] = {}
    for key, value in samples.items():
        name, _, label = key.partition('|')
        if name == 'bible_service_heartbeat_age_seconds' and value > heartbeat_stale:
            alerts[f'heartbeat:{label}'] = f'Служба {label} не отвечает {int(value // 60)} мин.'
        elif name == 'bible_loop_last_ok_age_seconds' and value > 300:
            alerts[f'loop:{label}'] = f'Цикл {label} не завершался без ошибок {int(value // 60)} мин.'
        elif name == 'bible_loop_duration_ms' and value > 120_000:
            alerts[f'loopslow:{label}'] = f'Цикл {label} выполняется слишком долго: {int(value / 1000)} с.'
        elif name == 'bible_backup_age_seconds' and value > backup_stale:
            alerts[f'backup:{label}'] = f'Последняя проверенная копия ({label}) старше {int(value // 3600)} ч.'
        elif name == 'bible_image_provider_blocked' and value >= 1:
            alerts[f'provider:{label}'] = f'Провайдер картинок {label} временно заблокирован.'
    if samples.get('bible_oldest_outstanding_delivery_seconds', 0) > stale_delivery:
        alerts['delivery:stuck'] = f"Есть неотправленная доставка старше {int(samples['bible_oldest_outstanding_delivery_seconds'] // 60)} мин."
    if samples.get('bible_deliveries|uncertain', 0) > 0:
        alerts['delivery:uncertain'] = f"Доставок с неопределённым исходом: {int(samples['bible_deliveries|uncertain'])} (нужно решение оператора)."
    if samples.get('bible_announcements_overdue', 0) > 0:
        alerts['announcements:overdue'] = f"Объявлений с опозданием запуска: {int(samples['bible_announcements_overdue'])} (проверьте службу composer)."
    if samples.get('bible_video_feeds_failing', 0) > 0:
        alerts['video:failing'] = f"Видеоканалов с ошибкой опроса: {int(samples['bible_video_feeds_failing'])}."
    if samples.get('bible_vk_inbox_pending', 0) > 500:
        alerts['vk:backlog'] = f"Очередь событий ВКонтакте растёт: {int(samples['bible_vk_inbox_pending'])}."
    limit = samples.get('bible_image_budget_limit_usd', 0)
    if limit and samples.get('bible_image_budget_spent_usd', 0) >= limit * budget_warn:
        alerts['budget:images'] = (f"Бюджет картинок: {samples['bible_image_budget_spent_usd']:.2f} из {limit:.2f} USD "
                                   f'({int(budget_warn * 100)}%+).')
    return alerts


def samples_from(exposition: Any) -> dict[str, float]:
    """Преобразовать собранные метрики в плоский словарь {`имя|метка`: значение}, удобный для `evaluate`."""
    result: dict[str, float] = {}
    for line in exposition.lines:
        if line.startswith('#'):
            continue
        head, _, value = line.rpartition(' ')
        name, _, tag = head.partition('{')
        label = tag.rstrip('}').split('="', 1)[1].rstrip('"') if tag else ''
        result[f'{name}|{label}' if label else name] = float(value)
    return result


def due(previous: dict[str, str], current: dict[str, str], now: datetime, repeat: timedelta) -> tuple[list[str], dict[str, str]]:
    """Решить, какие оповещения отправить сейчас: новые или не повторявшиеся дольше `repeat`, а также сообщения о восстановлении. Возвращает (список сообщений, новое сохраняемое состояние)."""
    messages, state = [], {}
    for alert_id, text in current.items():
        last = previous.get(alert_id)
        if last is None or now - datetime.fromisoformat(last) >= repeat:
            messages.append(text)
            state[alert_id] = now.isoformat()
        else:
            state[alert_id] = last
    resolved = [a for a in previous if a not in current]
    messages += [f'Восстановлено: {a}' for a in resolved]
    return messages, state


async def notify(settings: Settings, chat_id: int, text: str) -> None:
    """Отправить текст оповещения в указанный чат через Bot API Telegram; при ошибке HTTP выбрасывает исключение."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f'https://api.telegram.org/bot{settings.bot_token}/sendMessage',
                                     json={'chat_id': chat_id, 'text': '⚠️ BibleMessengerBot\n' + text})
        response.raise_for_status()


async def run() -> int:
    """Один проход проверки: собрать метрики, вычислить оповещения, отправить новые владельцу, сохранить состояние в `app_settings` и вывести краткий JSON-итог."""
    settings = Settings.from_env(require_bot_token=True)
    repeat = timedelta(hours=float(os.getenv('ALERT_REPEAT_HOURS', '6')))
    await wait_for_database(settings)
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as connection:
            samples = samples_from(await collect(connection, Decimal(os.getenv('IMAGE_MONTHLY_BUDGET_USD', '10'))))
            current = evaluate(samples)
            raw = await connection.fetchval("SELECT value FROM app_settings WHERE key=$1", STATE_KEY)
            previous = json.loads(raw) if raw else {}
            messages, state = due(previous, current, datetime.now(UTC), repeat)
            owner = await connection.fetchval('SELECT telegram_user_id FROM telegram_users WHERE is_owner')
            if messages and owner:
                await notify(settings, owner, '\n'.join(messages))
            elif messages:
                LOGGER.warning('alerts without owner: %s', messages)
            await connection.execute("""INSERT INTO app_settings(key,value) VALUES($1,$2)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", STATE_KEY, json.dumps(state))
        print(json.dumps({'active': sorted(current), 'sent': len(messages)}, ensure_ascii=False))
        return 0
    finally:
        await close_pool()


if __name__ == '__main__':
    from app.logging import configure_logging
    configure_logging()
    raise SystemExit(asyncio.run(run()))
