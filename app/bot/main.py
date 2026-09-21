"""aiogram long polling, localized command descriptions and a singleton guard."""
from __future__ import annotations
import asyncio
import logging
from aiogram import Bot,Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from app.bot.handlers import router
from app.config import Settings
from app.db import close_pool,create_pool,wait_for_database,acquire_runtime_guard
from app.logging import configure_logging
from app.services.i18n import available_ui,tr
from app.services.locks import lock_key

LOGGER = logging.getLogger(__name__)


async def heartbeat(pool) -> None:
    """A failed heartbeat terminates polling through the shared task supervisor."""
    while True:
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO service_heartbeats(service) VALUES('bot') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
        await asyncio.sleep(15)

COMMAND_KEYS = [('start','start'),('settings','settings'),('today','today'),('next','next'),
    ('random','random'),('topics','topics'),('translations','edition'),('subscribe','mode'),
    ('status','status'),('pause','pause'),('resume','resume'),('unsubscribe','unsubscribe'),('help','help')]


async def main() -> None:
    """A single polling process per database; no public webhook/domain is required."""
    settings = Settings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    bot = Bot(settings.bot_token,default=DefaultBotProperties(parse_mode='HTML'))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher['db_pool'],dispatcher['settings'] = pool,settings
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-poller',1)):
                raise RuntimeError('Another polling bot uses this database')
            # Never delete an existing webhook silently: the token may belong to another deployment.
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise RuntimeError('This bot has an active webhook; deliberately remove it before switching to polling')
            for locale in ['']+list(available_ui()):
                await bot.set_my_commands([BotCommand(command=command,description=tr(locale or 'en',key)[:256])
                    for command,key in COMMAND_KEYS],language_code=locale)
                await asyncio.sleep(0.1)
            tasks = [asyncio.create_task(heartbeat(pool)),asyncio.create_task(dispatcher.start_polling(
                bot,allowed_updates=dispatcher.resolve_used_update_types(),close_bot_session=False,
                handle_as_tasks=True,tasks_concurrency_limit=16))]
            try:
                done,pending = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
    finally:
        await bot.session.close()
        await close_pool()


if __name__=='__main__':
    configure_logging()
    asyncio.run(main())
