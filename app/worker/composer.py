"""Служба подготовки молитв и запуска объявлений по расписанию: работает заранее и не мешает срочной отправке сообщений."""
import asyncio
import contextlib
import logging

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.net import store as net_store
from app.services import announcements, prayers
from app.services import loops as loops_module
from app.services.locks import lock_key

LOGGER=logging.getLogger(__name__)


async def heartbeat(pool):
    """Каждые 20 секунд записывать в БД сигнал жизни службы `composer` для проверки работоспособности."""
    while True:
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO service_heartbeats(service) VALUES('composer') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(20)


async def run_video_watch(connection):
    """Опросить видеоканалы, срок которых наступил (сбой фиксируется в статистике цикла и не останавливает остальные циклы)."""
    from app.services import video_watch
    await video_watch.run_due(connection)


async def recover_delivery(connection):
    """Recover committed publication/broadcast intent independently of new schedules."""
    from app.services import broadcast, chat_sources
    await broadcast.recover(connection)
    await chat_sources.retry_publications(connection)


async def worker():
    """Запустить службу: взять блокировки (единственный экземпляр), запустить сигнал жизни и каждые 10 секунд готовить приглашения на молитву, срок которых близок."""
    settings=Settings.from_env(require_bot_token=False)
    await wait_for_database(settings)
    pool=await create_pool(settings)
    beat=None
    netloop=None
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            await net_store.refresh(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-composer',1)):
                raise RuntimeError('Another prayer composer is running')
            beat=asyncio.create_task(heartbeat(pool))
            netloop=asyncio.create_task(net_store.refresh_loop(pool))
            # Независимые циклы: медленный опрос видео не задерживает объявления и молитвы; итоги проходов пишутся в loop_stats.
            loops = [asyncio.create_task(loops_module.run_forever(pool, name, step, interval))
                     for name, step, interval in (('prayers', prayers.prepare_due, 10), ('announcements', announcements.run_due, 10),
                                                  ('videos', run_video_watch, 30), ('delivery_recovery', recover_delivery, 10))]
            try:
                await asyncio.gather(*loops)
            finally:
                for task in loops:
                    task.cancel()
                await asyncio.gather(*loops, return_exceptions=True)
    finally:
        for task in (beat, netloop):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await close_pool()


if __name__=='__main__':
    from app.logging import configure_logging
    configure_logging()
    asyncio.run(worker())
