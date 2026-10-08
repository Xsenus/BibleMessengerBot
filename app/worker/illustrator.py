"""Prepare tomorrow's artwork independently of the time-critical Telegram worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.services import artwork, cloud_backup
from app.services.locks import lock_key

LOGGER = logging.getLogger(__name__)


async def heartbeat(pool):
    while True:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO service_heartbeats(service) VALUES('illustrator') ON CONFLICT(service) DO UPDATE SET last_seen=now()"
            )
        await asyncio.sleep(20)


async def mirror(pool, cloud):
    while True:
        try:
            async with pool.acquire() as c:
                await cloud_backup.backup_images(c, cloud)
        except Exception as error:
            LOGGER.warning("Cloud artwork backup deferred (%s)", type(error).__name__)
        await asyncio.sleep(30)


async def worker():
    settings = Settings.from_env(require_bot_token=False)
    art = artwork.ArtSettings.from_env()
    cloud = cloud_backup.CloudSettings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    beat = None
    copies = None
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            if not await owner.fetchval(
                "SELECT pg_try_advisory_lock($1)", lock_key("singleton-illustrator", 1)
            ):
                raise RuntimeError("Another illustrator is running")
            await owner.execute(
                "UPDATE image_generation_jobs SET state='uncertain',error_code='worker_interrupted',updated_at=now() WHERE state='running'"
            )
            await owner.execute(
                "UPDATE image_generation_attempts SET state='uncertain' WHERE state='reserved'"
            )
            fingerprint = hashlib.sha256(art.key.encode()).hexdigest()
            previous = await owner.fetchval(
                "SELECT value FROM app_settings WHERE key='imagegen-key-fingerprint'"
            )
            if fingerprint != previous:
                await owner.execute(
                    "UPDATE image_generation_jobs SET state='queued',retry_at=NULL,error_code=NULL WHERE state='failed' AND error_code IN ('auth','missing_key')"
                )
                await owner.execute(
                    "INSERT INTO app_settings(key,value) VALUES('imagegen-key-fingerprint',$1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    fingerprint,
                )
            beat = asyncio.create_task(heartbeat(pool))
            copies = asyncio.create_task(mirror(pool, cloud))
            while True:
                async with pool.acquire() as c:
                    await artwork.plan_ahead(c)
                    if art.provider == "openai" and art.key:
                        # Pause after authentication failure until the key is changed.
                        auth_blocked = await c.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM image_generation_jobs WHERE state='failed' AND error_code='auth')"
                        )
                        if not auth_blocked:
                            jobs = await c.fetch("""SELECT j.id,min(t.scheduled_for) AS needed_at FROM image_generation_jobs j
                                JOIN image_generation_targets t ON t.image_id=j.image_id
                                JOIN subscriptions s ON s.id=t.subscription_id
                                JOIN telegram_chats c ON c.telegram_chat_id=s.telegram_chat_id
                                JOIN verse_illustrations i ON i.id=j.image_id
                                WHERE j.state IN ('queued','retry') AND j.attempts<3 AND (j.retry_at IS NULL OR j.retry_at<=now())
                                AND s.is_enabled AND NOT s.completed AND s.next_run_at IS NOT NULL AND c.is_active AND s.translation_id=i.translation_id
                                AND extract(isodow from t.local_date)::int=ANY(s.days_of_week)
                                AND t.local_date >= (now() AT TIME ZONE s.timezone)::date
                                GROUP BY j.id ORDER BY needed_at,j.id LIMIT 6""")
                            for job in jobs:
                                outcome = await artwork.process_job(c, job["id"], art)
                                LOGGER.info("Artwork job %s: %s", job["id"], outcome)
                                if outcome in {"auth", "budget_or_not_due"}:
                                    break
                await asyncio.sleep(60)
    finally:
        for task in (beat, copies):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await close_pool()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(worker())
