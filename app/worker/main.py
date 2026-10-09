"""Single active scheduler with independent preparation and bounded concurrent sends."""
from __future__ import annotations

import asyncio
import signal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from app.bot.transport import TelegramSender
from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.services.locks import lock_key
from app.worker.delivery import recover_ambiguous
from app.worker.scheduler import prepare_loop, send_loop


async def heartbeat(owner):
    """Check the singleton's owning session even during slow preparation/sends."""
    while True:
        await owner.execute("INSERT INTO service_heartbeats(service) VALUES('worker') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(15)


async def worker() -> None:
    settings = Settings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode='HTML'))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    tasks = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
            except NotImplementedError:
                pass  # Windows development; deployed Linux supports both signals.
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)', lock_key('singleton-worker', 1)):
                raise RuntimeError('Another delivery worker is already running for this database')
            await recover_ambiguous(owner)
            sender = asyncio.create_task(send_loop(pool, lambda connection: TelegramSender(bot, connection, settings), stop))
            shutdown = asyncio.create_task(stop.wait())
            tasks = [asyncio.create_task(heartbeat(owner)), asyncio.create_task(prepare_loop(pool, settings)), sender, shutdown]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if stop.is_set():
                    tasks[1].cancel()  # Keep owner/heartbeat until in-flight sends drain.
                    await sender
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        await bot.session.close()
        await close_pool()


if __name__ == '__main__':
    configure_logging()
    asyncio.run(worker())
