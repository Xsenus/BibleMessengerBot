"""Ограничитель скорости отправки платформы: общий и по чатам, общий для всех процессов (состояние хранится в БД)."""
from __future__ import annotations

import asyncio
from typing import Any

from app.platforms.spec import PlatformSpec


class RateLimiter:
    """Дожидается разрешения на отправку: не чаще `global_gap` между любыми сообщениями и `chat_gap` в один чат."""

    def __init__(self, spec: PlatformSpec) -> None:
        self.spec = spec
        self.table = spec.rate_table

    async def wait(self, connection: Any, chat_id: int) -> None:
        """Заблокировать слот отправки и при необходимости подождать."""
        key = f'chat:{chat_id}'
        table, global_gap, chat_gap = self.table, self.spec.global_gap, self.spec.chat_gap
        while True:
            async with connection.transaction():
                await connection.execute(f"INSERT INTO {table}(key) VALUES('global'),($1) ON CONFLICT DO NOTHING", key)  # noqa: S608
                await connection.fetchrow(f"SELECT * FROM {table} WHERE key='global' FOR UPDATE")  # noqa: S608
                await connection.fetchrow(f'SELECT * FROM {table} WHERE key=$1 FOR UPDATE', key)  # noqa: S608
                delay = await connection.fetchval(f'''SELECT GREATEST(0,EXTRACT(EPOCH FROM
                    (MAX(next_allowed)-clock_timestamp())))::double precision FROM {table} WHERE key IN ('global',$1)''', key)  # noqa: S608
                if delay <= 0:
                    await connection.execute(f"UPDATE {table} SET next_allowed=clock_timestamp()+make_interval(secs=>$1) WHERE key='global'", global_gap)  # noqa: S608
                    await connection.execute(f'UPDATE {table} SET next_allowed=clock_timestamp()+make_interval(secs=>$2) WHERE key=$1', key, chat_gap)  # noqa: S608
                    return
            await asyncio.sleep(min(delay, 4))
