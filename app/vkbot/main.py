"""Служба ВКонтакте: Long Poll → надёжная входящая очередь → общие обработчики. Один активный экземпляр на базу."""
from __future__ import annotations

import asyncio
import logging
import signal

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.net import store as net_store
from app.services import features
from app.services.locks import lock_key
from app.vkbot.bridge import VkBotBridge
from app.vkbot.client import VkAPIError, VkClient
from app.vkbot.config import VkSettings
from app.vkbot.dispatcher import VkDispatcher
from app.vkbot.events import ingest
from app.vkbot.inbox import consumer_loop, recover

LOGGER = logging.getLogger(__name__)


async def heartbeat(connection) -> None:
    """Каждые 15 секунд записывать сигнал жизни службы `vkbot` через соединение-владельца блокировки."""
    while True:
        await connection.execute("INSERT INTO service_heartbeats(service) VALUES('vkbot') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(15)


async def poll_once(client: VkClient, pool, server: dict) -> dict:
    """Один шаг Long Poll. Возвращает (возможно обновлённый) сервер. Позиция `ts` сдвигается только после успешной записи
    событий в БД; сбои сети, API и БД не пробрасываются — шаг просто повторится."""
    try:
        body = await client.poll(server)
    except VkAPIError:
        await asyncio.sleep(3)
        return server
    failed = body.get('failed')
    if failed == 1:                                         # потеряна часть истории: взять новый ts
        return dict(server, ts=body.get('ts', server['ts']))
    if failed:                                              # 2 — ключ устарел, 3 — потеряна информация: получить сервер заново
        try:
            return await client.long_poll_server()
        except VkAPIError:
            await asyncio.sleep(3)
            return server
    updates = body.get('updates') or []
    if updates:
        try:
            async with pool.acquire() as connection, connection.transaction():
                for update in updates:
                    try:
                        await ingest(connection, update)
                    except ValueError:
                        LOGGER.warning('Unsupported VK event skipped')
        except Exception as error:
            LOGGER.error('VK events were not stored (%s); retrying', type(error).__name__)
            await asyncio.sleep(3)
            return server
    return dict(server, ts=body.get('ts', server['ts']))


async def poll_loop(client: VkClient, pool, stop: asyncio.Event) -> None:
    """Принимать события Long Poll и записывать их в `vk_inbox` одной транзакцией; повторные доставки дедуплицируются по `event_id`."""
    server = await client.long_poll_server()
    while not stop.is_set():
        server = await poll_once(client, pool, server)


async def main() -> None:
    """Запустить службу: проверить токен и настройки сообщества, взять блокировку единственного экземпляра, включить
    Long Poll и параллельно выполнять приём событий, их обработку и сигнал жизни."""
    settings = Settings.from_env(require_bot_token=False)
    config = VkSettings.from_env(require_token=True)
    if not config.group_id:
        raise RuntimeError('VK_GROUP_ID is required')
    await wait_for_database(settings)
    pool = await create_pool(settings)
    client = VkClient(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    tasks: list[asyncio.Task] = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stop.set)
                installed.append(signum)
            except NotImplementedError:
                pass
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)', lock_key('singleton-vkbot', 1)):
                raise RuntimeError('Another VK consumer is running')
            info = await client.get_me()
            await client.configure_long_poll()
            bridge = VkBotBridge(client, pool, info)
            await recover(owner)
            dispatcher = VkDispatcher(bridge, pool, settings)
            consumer = asyncio.create_task(consumer_loop(pool, dispatcher, stop))
            tasks = [asyncio.create_task(heartbeat(owner)), consumer, asyncio.create_task(features.refresh_loop(pool)), asyncio.create_task(net_store.refresh_loop(pool)), asyncio.create_task(poll_loop(client, pool, stop)),
                     asyncio.create_task(stop.wait())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if stop.is_set():
                await consumer
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)
        await client.close()
        await close_pool()


if __name__ == '__main__':
    configure_logging()
    asyncio.run(main())
