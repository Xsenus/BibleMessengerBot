"""Включаемые разделы бота (временно скрытые функции).

Значение по умолчанию берётся из переменных окружения; администратор может переопределить его в панели — переопределение
хранится в `app_settings` и подхватывается каждым сервисом не позднее чем через 30 секунд, без перезапуска.
"""
from __future__ import annotations

import asyncio
import logging
import os

LOGGER = logging.getLogger(__name__)
SUPPORT_KEY = 'feature:support_section'
TRUE = {'1', 'true', 'yes', 'on'}
_override: dict[str, bool] = {}


def support_enabled() -> bool:
    """Показан ли раздел помощи проекту (/donate, /donations, /paysupport, /terms, кнопка «Поддержать»).

    Раздел временно скрыт: включается в панели или переменной SUPPORT_SECTION_ENABLED=true. Обработка уже
    поступающих платежей и возвратов не отключается, чтобы не потерять деньги в пути.
    """
    if SUPPORT_KEY in _override:
        return _override[SUPPORT_KEY]
    return os.getenv('SUPPORT_SECTION_ENABLED', 'false').strip().lower() in TRUE


async def refresh(connection) -> None:
    """Прочитать переопределения из БД (отсутствие записи — вернуться к значению из окружения)."""
    value = await connection.fetchval('SELECT value FROM app_settings WHERE key=$1', SUPPORT_KEY)
    if value is None:
        _override.pop(SUPPORT_KEY, None)
    else:
        _override[SUPPORT_KEY] = str(value).lower() in TRUE


async def refresh_loop(pool, interval: float = 30) -> None:
    """Фоновое обновление переопределений; сбой БД не останавливает службу (остаётся прежнее значение)."""
    while True:
        try:
            async with pool.acquire() as connection:
                await refresh(connection)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning('Feature flags refresh deferred (%s)', type(error).__name__)
        await asyncio.sleep(interval)


async def set_support(connection, enabled: bool) -> None:
    """Сохранить переопределение раздела поддержки (вызывается из панели)."""
    await connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', SUPPORT_KEY, 'true' if enabled else 'false')
    _override[SUPPORT_KEY] = enabled
