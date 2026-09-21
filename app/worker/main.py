"""Single active scheduler/sender with crash-safe delivery checkpoints."""
from __future__ import annotations
import asyncio
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from app.bot.transport import TelegramSender
from app.config import Settings
from app.db import close_pool,create_pool,wait_for_database,acquire_runtime_guard
from app.logging import configure_logging
from app.services.locks import lock_key
from app.worker.delivery import prepare_subscription,process_delivery,recover_ambiguous

LOGGER = logging.getLogger(__name__)


async def worker() -> None:
    """Hold the worker lock for process lifetime; database faults trigger a clean restart."""
    settings = Settings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    bot = Bot(settings.bot_token,default=DefaultBotProperties(parse_mode='HTML'))
    try:
        async with pool.acquire() as owner_connection:
            await acquire_runtime_guard(owner_connection)
            acquired = await owner_connection.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-worker',1))
            if not acquired:
                raise RuntimeError('Another delivery worker is already running for this database')
            await recover_ambiguous(owner_connection)
            while True:
                await owner_connection.execute("INSERT INTO service_heartbeats(service) VALUES('worker') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
                async with pool.acquire() as connection:
                    due = await connection.fetch('''SELECT s.id FROM subscriptions s JOIN telegram_chats c
                        ON c.telegram_chat_id=s.telegram_chat_id WHERE s.is_enabled AND NOT s.completed AND c.is_active
                        AND s.next_run_at<=now() AND NOT EXISTS(SELECT 1 FROM delivery_log d
                            WHERE d.subscription_id=s.id AND d.status IN ('pending','sending','retry','uncertain'))
                        ORDER BY s.next_run_at LIMIT 10''')
                    for item in due:
                        try:
                            await prepare_subscription(connection,item['id'],settings.max_message_length)
                        except (ValueError,KeyError) as error:
                            # Permanent content/configuration errors need operator attention, not a retry loop.
                            await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE id=$1',item['id'])
                            await connection.execute("INSERT INTO operator_events(action,details) VALUES('schedule_blocked',jsonb_build_object('subscription_id',$1::bigint,'error',$2::text))",item['id'],type(error).__name__)
                            LOGGER.warning('Schedule %s paused: %s',item['id'],type(error).__name__)
                    jobs = await connection.fetch("SELECT d.id FROM delivery_log d JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id LEFT JOIN subscriptions s ON s.id=d.subscription_id WHERE d.status IN ('pending','retry') AND (d.retry_at IS NULL OR d.retry_at<=now()) AND c.is_active AND (d.subscription_id IS NULL OR s.is_enabled) ORDER BY d.updated_at,d.id LIMIT 20")
                    for job in jobs:
                        await process_delivery(connection,job['id'],TelegramSender(bot,connection,settings))
                await asyncio.sleep(0.25 if jobs else settings.worker_poll_seconds)
    finally:
        await bot.session.close()
        await close_pool()


if __name__=='__main__':
    configure_logging()
    asyncio.run(worker())
