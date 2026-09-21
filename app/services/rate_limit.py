"""Database-backed send throttling shared by the polling bot and delivery worker."""
from __future__ import annotations
import asyncio
from typing import Any


def intervals(chat_id: int, global_rate: float = 20, chat_rate: float = 1) -> tuple[float,float]:
    """Enforce conservative free-tier ceilings even with unsafe environment values."""
    if not 0 < global_rate <= 20 or not 0 < chat_rate <= 1:
        raise ValueError('Rates must be positive, global <=20/s, chat <=1/s')
    return 1/global_rate,max(1/chat_rate,3.2 if chat_id < 0 else 1.05)


async def wait_send_slot(connection: Any, chat_id: int, global_rate: float = 20,
                         chat_rate: float = 1) -> None:
    """Reserve only a currently available slot, avoiding long future reservations.

    All callers lock the global row before a chat row, preventing lock-order
    deadlocks. Transactions are short and never include the sleep or API request.
    """
    global_interval,chat_interval = intervals(chat_id,global_rate,chat_rate)
    key = f'chat:{chat_id}'
    while True:
        async with connection.transaction():
            await connection.execute("INSERT INTO telegram_rate_limits(key) VALUES('global'),($1) ON CONFLICT DO NOTHING",key)
            await connection.fetchrow("SELECT * FROM telegram_rate_limits WHERE key='global' FOR UPDATE")
            await connection.fetchrow('SELECT * FROM telegram_rate_limits WHERE key=$1 FOR UPDATE',key)
            delay = await connection.fetchval('''SELECT GREATEST(0,EXTRACT(EPOCH FROM
                (MAX(next_allowed)-clock_timestamp())))::double precision
                FROM telegram_rate_limits WHERE key IN ('global',$1)''',key)
            if delay <= 0:
                await connection.execute("UPDATE telegram_rate_limits SET next_allowed=clock_timestamp()+$1*interval '1 second' WHERE key='global'",global_interval)
                await connection.execute("UPDATE telegram_rate_limits SET next_allowed=clock_timestamp()+$2*interval '1 second' WHERE key=$1",key,chat_interval)
                return
        await asyncio.sleep(min(delay,4))
