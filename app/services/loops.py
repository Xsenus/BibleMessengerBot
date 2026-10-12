"""Независимые циклы службы с учётом длительности и ошибок.

Каждый цикл (молитвы, объявления, видео…) работает в своей задаче и со своим интервалом: медленный опрос видео не задерживает
объявления. Итоги каждого прохода записываются в `loop_stats` — оттуда берутся метрики и оповещения."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


async def record(pool: Any, name: str, duration_ms: int, error: str) -> None:
    """Записать итог одного прохода цикла (сбой записи статистики не должен мешать работе)."""
    try:
        async with pool.acquire() as connection:
            await connection.execute('''INSERT INTO loop_stats(name,runs,errors,last_run_at,last_ok_at,last_duration_ms,last_error)
                VALUES($1,1,$2::int,now(),CASE WHEN $2::int=0 THEN now() END,$3::int,$4)
                ON CONFLICT(name) DO UPDATE SET runs=loop_stats.runs+1,errors=loop_stats.errors+$2::int,last_run_at=now(),
                    last_ok_at=CASE WHEN $2::int=0 THEN now() ELSE loop_stats.last_ok_at END,last_duration_ms=$3::int,last_error=$4''',
                                     name, 1 if error else 0, duration_ms, error)
    except Exception as exc:
        LOGGER.warning('Loop stats not stored (%s)', type(exc).__name__)


async def run_forever(pool: Any, name: str, step: Callable[[Any], Awaitable[Any]], interval: float) -> None:
    """Выполнять `step(connection)` каждые `interval` секунд; сбой шага записывается и не останавливает цикл."""
    while True:
        started = time.monotonic()
        error = ''
        try:
            async with pool.acquire() as connection:
                await step(connection)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
            LOGGER.error('Loop %s failed (%s)', name, error)
        elapsed = time.monotonic() - started
        await record(pool, name, int(elapsed * 1000), error)
        await asyncio.sleep(max(1.0, interval - elapsed))
