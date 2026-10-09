"""MAX rate reservations are independent of Telegram's shared rate budget."""
from __future__ import annotations

import asyncio
from typing import Any


async def wait_send_slot(connection: Any, chat_id: int) -> None:
    """Conservative 10 global/second and 1 per chat/second, shared across processes."""
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
