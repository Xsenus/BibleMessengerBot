"""PostgreSQL-backed scheduled delivery worker."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import asyncpg
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings
from app.db import close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.worker.delivery import RateLimiter, deliver_one

LOGGER = logging.getLogger(__name__)


async def claim_due(pool: asyncpg.Pool, limit: int = 20) -> list[asyncpg.Record]:
    token = uuid4()
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            """
            SELECT s.*
            FROM subscriptions s
            JOIN telegram_chats c ON c.telegram_chat_id=s.telegram_chat_id
            WHERE s.is_enabled=true AND c.is_active=true
              AND s.next_run_at IS NOT NULL AND s.next_run_at <= now()
              AND (s.locked_at IS NULL OR s.locked_at < now() - interval '15 minutes')
            ORDER BY s.next_run_at
            FOR UPDATE SKIP LOCKED
            LIMIT $1
            """,
            limit,
        )
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        await connection.execute(
            """
            UPDATE subscriptions SET locked_at=now(), lock_token=$2
            WHERE id = ANY($1::bigint[])
            """,
            ids,
            token,
        )
        return await connection.fetch(
            "SELECT * FROM subscriptions WHERE id = ANY($1::bigint[]) ORDER BY next_run_at",
            ids,
        )


async def worker() -> None:
    settings = Settings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    global_limiter = RateLimiter(settings.telegram_global_rate_per_second)
    chat_limiters: dict[int, RateLimiter] = {}
    LOGGER.info("Delivery worker started")
    try:
        while True:
            due = await claim_due(pool)
            if not due:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            for subscription in due:
                async with pool.acquire() as connection:
                    try:
                        await deliver_one(
                            bot,
                            connection,
                            subscription,
                            settings,
                            global_limiter,
                            chat_limiters,
                        )
                    except Exception:  # noqa: BLE001 - isolate one subscription from the loop
                        LOGGER.exception("Uncaught delivery error for subscription %s", subscription["id"])
                        await connection.execute(
                            """
                            UPDATE subscriptions SET locked_at=NULL, lock_token=NULL,
                                next_run_at=now()+interval '5 minutes', updated_at=now()
                            WHERE id=$1
                            """,
                            subscription["id"],
                        )
    finally:
        await bot.session.close()
        await close_pool()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(worker())
