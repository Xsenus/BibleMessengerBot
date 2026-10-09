"""Prepare ahead of time and dispatch a bounded number of independent chats."""
from __future__ import annotations

import asyncio
import logging

from app.services import scheduled_media
from app.services.locks import chat_lock
from app.worker.delivery import prepare_subscription, process_delivery

LOGGER = logging.getLogger(__name__)
SEND_CONCURRENCY = 4  # Pool size 10: owner, preparation, selector and four sends.
SEND_POLL_SECONDS = 0.25


async def prepare_once(connection, settings):
    due = await connection.fetch('''SELECT s.id FROM subscriptions s JOIN telegram_chats c
        ON c.telegram_chat_id=s.telegram_chat_id WHERE s.is_enabled AND NOT s.completed AND c.is_active
        AND s.next_run_at<=now()+interval '24 hours' AND NOT EXISTS(SELECT 1 FROM delivery_log d
            WHERE d.subscription_id=s.id AND d.status IN ('pending','sending','retry','uncertain'))
        ORDER BY s.next_run_at LIMIT 10''')
    for item in due:
        try:
            await prepare_subscription(connection, item['id'], settings.max_message_length, lead_seconds=86400)
        except (ValueError, KeyError) as error:
            await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE id=$1', item['id'])
            await connection.execute("INSERT INTO operator_events(action,details) VALUES('schedule_blocked',jsonb_build_object('subscription_id',$1::bigint,'error',$2::text))", item['id'], type(error).__name__)
            LOGGER.warning('Schedule %s paused: %s', item['id'], type(error).__name__)
    assembling = await connection.fetch('''SELECT d.id,d.telegram_chat_id FROM delivery_log d
        JOIN scheduled_readings r ON r.delivery_id=d.id
        JOIN subscriptions s ON s.id=d.subscription_id JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id
        WHERE r.state='preparing' AND d.status IN ('pending','retry') AND s.is_enabled AND c.is_active
        AND s.revision=d.subscription_revision AND c.revision=d.chat_revision
        ORDER BY r.updated_at,d.scheduled_for LIMIT 20''')
    for item in assembling:
        # Inspect again under the destination lock: sending and settings now
        # run concurrently with preparation of the frozen delivery bundle.
        async with chat_lock(connection, item['telegram_chat_id']):
            delivery = await connection.fetchrow('''SELECT d.* FROM delivery_log d
                JOIN scheduled_readings r ON r.delivery_id=d.id
                JOIN subscriptions s ON s.id=d.subscription_id
                JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id
                WHERE d.id=$1 AND r.state='preparing' AND d.status IN ('pending','retry')
                AND s.is_enabled AND c.is_active AND s.revision=d.subscription_revision AND c.revision=d.chat_revision''', item['id'])
            if delivery:
                await scheduled_media.ready(connection, delivery)
                await connection.execute("UPDATE scheduled_readings SET updated_at=now() WHERE delivery_id=$1 AND state='preparing'", item['id'])


async def prepare_loop(pool, settings):
    while True:
        async with pool.acquire() as connection:
            await prepare_once(connection, settings)
        await asyncio.sleep(max(0.25, settings.worker_poll_seconds))


async def due_jobs(connection, busy_chats, limit):
    """One job per free chat, with prayers first; incomplete media cannot starve others."""
    return await connection.fetch('''SELECT id,telegram_chat_id FROM (
        SELECT DISTINCT ON (d.telegram_chat_id) d.id,d.telegram_chat_id,d.mode,d.scheduled_for
        FROM delivery_log d JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id
        LEFT JOIN subscriptions s ON s.id=d.subscription_id
        LEFT JOIN scheduled_readings r ON r.delivery_id=d.id
        WHERE d.status IN ('pending','retry') AND d.scheduled_for<=now()
        AND (d.retry_at IS NULL OR d.retry_at<=now()) AND c.is_active
        AND (d.subscription_id IS NULL OR s.is_enabled)
        AND NOT (d.telegram_chat_id=ANY($1::bigint[]))
        AND (r.delivery_id IS NULL OR r.state='ready' OR (r.state='preparing' AND r.deadline_at<=now()))
        ORDER BY d.telegram_chat_id,(d.mode='prayer') DESC,d.scheduled_for,d.id
        ) candidates ORDER BY (mode='prayer') DESC,scheduled_for,id LIMIT $2''', list(busy_chats), limit)


async def send_loop(pool, sender_factory, stop, *, concurrency=SEND_CONCURRENCY, poll=SEND_POLL_SECONDS):
    """A slow chat occupies one slot; other chats continue without waiting for a batch."""
    active = {}

    async def send_one(job):
        async with pool.acquire() as connection:
            result = await process_delivery(connection, job['id'], sender_factory(connection))
            if result == 'preparing_media':
                # A previously ready asset can be quarantined while queued.
                # Recheck later without monopolizing the first dispatch slots.
                await connection.execute("UPDATE delivery_log SET retry_at=now()+interval '5 seconds' WHERE id=$1 AND status IN ('pending','retry')", job['id'])
            return result

    try:
        while not stop.is_set():
            for chat_id, task in list(active.items()):
                if task.done():
                    task.result()  # DB/programming failures stop the owner, rather than being lost.
                    del active[chat_id]
            if len(active) < concurrency:
                async with pool.acquire() as connection:
                    jobs = await due_jobs(connection, active, concurrency-len(active))
                if stop.is_set():
                    break
                for job in jobs:
                    active[job['telegram_chat_id']] = asyncio.create_task(send_one(job))
            if active:
                await asyncio.wait(active.values(), timeout=poll, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(poll)
        # Stop taking work first; save acknowledgements before the container's
        # 45-second shutdown deadline. Unfinished requests retain SQL checkpoints.
        if active:
            done, _ = await asyncio.wait(active.values(), timeout=40)
            for task in done:
                task.result()
    finally:
        for task in active.values():
            task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
