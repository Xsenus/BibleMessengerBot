"""Служба подготовки молитв и запуска объявлений по расписанию: работает заранее и не мешает срочной отправке сообщений."""
import asyncio
import contextlib
import logging

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.net import store as net_store
from app.services import announcements, prayers
from app.services.locks import lock_key

LOGGER=logging.getLogger(__name__)


async def heartbeat(pool):
    """Каждые 20 секунд записывать в БД сигнал жизни службы `composer` для проверки работоспособности."""
    while True:
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO service_heartbeats(service) VALUES('composer') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(20)


async def run_video_watch(connection):
    """Опросить видеоканалы, срок которых наступил; сбой слежения не должен останавливать подготовку молитв и объявления."""
    try:
        from app.services import video_watch
        await video_watch.run_due(connection)
    except Exception as error:
        LOGGER.error('Video watch failed (%s)', type(error).__name__)


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
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-composer',1)):
                raise RuntimeError('Another prayer composer is running')
            beat=asyncio.create_task(heartbeat(pool))
            netloop=asyncio.create_task(net_store.refresh_loop(pool))
            while True:
                async with pool.acquire() as connection:
                    # Каждый шаг изолирован: сбой объявлений не останавливает молитвы и наоборот.
                    for step in (prayers.prepare_due, announcements.run_due, run_video_watch):
                        try:
                            await step(connection)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            LOGGER.error('Composer step %s failed (%s)', getattr(step, '__name__', 'step'), type(error).__name__)
                await asyncio.sleep(10)
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
