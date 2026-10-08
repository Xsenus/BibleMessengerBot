"""News/prayer preparation is isolated from time-critical Telegram sends."""
import asyncio
import contextlib
import logging

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.services import prayers
from app.services.locks import lock_key

LOGGER=logging.getLogger(__name__)


async def heartbeat(pool):
    while True:
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO service_heartbeats(service) VALUES('composer') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(20)


async def worker():
    settings=Settings.from_env(require_bot_token=False)
    await wait_for_database(settings)
    pool=await create_pool(settings)
    beat=None
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-composer',1)):
                raise RuntimeError('Another prayer composer is running')
            beat=asyncio.create_task(heartbeat(pool))
            while True:
                async with pool.acquire() as connection:
                    await prayers.prepare_due(connection)
                await asyncio.sleep(10)
    finally:
        if beat:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        await close_pool()


if __name__=='__main__':
    from app.logging import configure_logging
    configure_logging()
    asyncio.run(worker())
