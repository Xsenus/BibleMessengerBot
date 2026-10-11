"""Ограничитель скорости MAX: отдельные резервы, не зависящие от общего бюджета Telegram."""
from __future__ import annotations

import asyncio
from typing import Any


async def wait_send_slot(connection: Any, chat_id: int) -> None:
    """Дождаться разрешения на отправку: не более 10 сообщений в секунду всего и 1 в секунду в чат, общие для всех процессов (состояние хранится в БД)."""
    key = f'chat:{chat_id}'
    while True:
        async with connection.transaction():
            await connection.execute("INSERT INTO max_rate_limits(key) VALUES('global'),($1) ON CONFLICT DO NOTHING", key)
            await connection.fetchrow("SELECT * FROM max_rate_limits WHERE key='global' FOR UPDATE")
            await connection.fetchrow('SELECT * FROM max_rate_limits WHERE key=$1 FOR UPDATE', key)
            delay = await connection.fetchval('''SELECT GREATEST(0,EXTRACT(EPOCH FROM
                (MAX(next_allowed)-clock_timestamp())))::double precision
                FROM max_rate_limits WHERE key IN ('global',$1)''', key)
            if delay <= 0:
                await connection.execute("UPDATE max_rate_limits SET next_allowed=clock_timestamp()+interval '0.1 second' WHERE key='global'")
                await connection.execute("UPDATE max_rate_limits SET next_allowed=clock_timestamp()+interval '1.05 seconds' WHERE key=$1", key)
                return
        await asyncio.sleep(min(delay, 4))
