"""Prepare tomorrow's artwork independently of the time-critical Telegram worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.services import artwork, cloud_backup, image_router
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
    if art.provider == 'openai':
        art = replace(art, provider_order=('openai',))
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
            await image_router.recover(owner)
            if art.provider in {'auto', 'openai'}:
                await image_router.configure(owner, art)
            beat = asyncio.create_task(heartbeat(pool))
            copies = asyncio.create_task(mirror(pool, cloud))
            while True:
                async with pool.acquire() as c:
                    await artwork.upgrade_queued_prompts(c)
                    await artwork.plan_ahead(c)
                    await artwork.dispatch_requests(c)
                    if art.provider in {'auto', 'openai'}:
                        jobs = await artwork.due_jobs(c, max_attempts=image_router.attempt_limit(art))
                        for job in jobs:
                            outcome = await image_router.process_job(c, job['id'], art)
                            LOGGER.info('Artwork job %s: %s', job['id'], outcome)
                            await artwork.dispatch_requests(c)
                await asyncio.sleep(10)
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
