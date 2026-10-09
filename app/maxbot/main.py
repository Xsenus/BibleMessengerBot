"""Singleton MAX consumer and idempotent command menu; HTTP receiver is separate."""
from __future__ import annotations

import asyncio
import signal

from app.bot.branding import commands_for
from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.maxbot.bridge import MaxBotBridge
from app.maxbot.client import MaxClient
from app.maxbot.config import MaxSettings
from app.maxbot.dispatcher import MaxDispatcher
from app.maxbot.events import UPDATE_TYPES
from app.maxbot.inbox import consumer_loop, recover
from app.payments.reconcile import loop as payment_loop
from app.services.locks import lock_key


async def heartbeat(connection):
    while True:
        await connection.execute("INSERT INTO service_heartbeats(service) VALUES('maxbot') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(15)


async def main():
    settings = Settings.from_env(require_bot_token=False)
    config = MaxSettings.from_env(require_token=True)
    if not config.webhook_url or not config.webhook_secret:
        raise RuntimeError('MAX webhook configuration is required')
    await wait_for_database(settings)
    pool = await create_pool(settings)
    client = MaxClient(config)
    stop = asyncio.Event()
    tasks, signals = [], []
    loop = asyncio.get_running_loop()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT):
            try:
                loop.add_signal_handler(signum,stop.set)
                signals.append(signum)
            except NotImplementedError:
                pass
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-maxbot',1)):
                raise RuntimeError('Another MAX consumer is running')
            info = await client.get_me()
            if type(info.get('user_id')) is not int or info['user_id']<=0 or not info.get('is_bot'):
                raise RuntimeError('MAX token does not identify a bot')
            bridge = MaxBotBridge(client,pool,info)
            await client.commands([{'name':c.command,'description': 'Поддержать картой или СБП' if c.command=='donate' else c.description} for c in commands_for('ru')])
            subscriptions = await client.request('GET','/subscriptions')
            existing = [s for s in subscriptions.get('subscriptions',[]) if s.get('url')!=config.webhook_url]
            if existing:
                raise RuntimeError('MAX bot has a different webhook; deliberately resolve before activation')
            await client.subscribe(list(UPDATE_TYPES))
            await recover(owner)
            dispatcher = MaxDispatcher(bridge,pool,settings)
            consumer = asyncio.create_task(consumer_loop(pool,dispatcher,stop))
            tasks = [asyncio.create_task(heartbeat(owner)),consumer,asyncio.create_task(payment_loop(pool,stop)),asyncio.create_task(stop.wait())]
            done,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if stop.is_set():
                await consumer
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for signum in signals:
            loop.remove_signal_handler(signum)
        await client.close()
        await close_pool()


if __name__=='__main__':
    configure_logging()
    asyncio.run(main())
