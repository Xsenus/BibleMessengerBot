"""Disposable PostgreSQL outbox and fake network: no messages to real users."""
import asyncio
import os
from collections import Counter
from itertools import pairwise

import pytest

from app.services.rate_limit import wait_send_slot
from app.worker.delivery import insert_payload
from app.worker.scheduler import due_jobs, prepare_once, send_loop
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db
from tests.test_scheduled_media_postgres import finish, setup

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


async def queued(connection, edition, chat_id, key, mode='fixture'):
    chat = await create_destination(connection, chat_id)
    return await insert_payload(connection, chat, edition, 'Synthetic scheduler fixture', key, mode, {'kind':'fixture'},
                                frozen_chunks=[dict(kind='rich', text='Synthetic scheduler fixture', image_id=None)])


async def test_queue_prioritizes_prayer_and_excludes_busy_future_retry_and_inactive(db):
    c, _, path = db
    edition, _, _ = await load_fixture(c, path)
    await queued(c, edition, 101, 'old-reading')
    prayer = await queued(c, edition, 101, 'prayer', 'prayer')
    future = await queued(c, edition, 102, 'future')
    retry = await queued(c, edition, 103, 'retry')
    await queued(c, edition, 104, 'inactive')
    ordinary = await queued(c, edition, 105, 'ordinary')
    await c.execute("UPDATE delivery_log SET scheduled_for=now()+interval '1 hour' WHERE id=$1", future)
    await c.execute("UPDATE delivery_log SET status='retry',retry_at=now()+interval '1 hour' WHERE id=$1", retry)
    await c.execute('UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=104')
    assert [r['id'] for r in await due_jobs(c, [], 20)] == [prayer, ordinary]
    assert [r['id'] for r in await due_jobs(c, [101], 20)] == [ordinary]
    assert [r['id'] for r in await due_jobs(c, [], 1)] == [prayer]


async def test_incomplete_bundle_is_skipped_then_background_marks_it_ready(db):
    c, settings, path = db
    _, identifier, _ = await setup(c, path, future=False)
    assert not await due_jobs(c, [], 4)
    await finish(c, identifier)
    await prepare_once(c, settings)
    assert [r['id'] for r in await due_jobs(c, [], 4)] == [identifier]


async def test_real_queue_parallel_sends_once_per_chat_and_drains_on_shutdown(db):
    import asyncpg
    c, settings, path = db
    edition, _, _ = await load_fixture(c, path)
    for chat in range(101, 113):
        await queued(c, edition, chat, f'first:{chat}')
    await queued(c, edition, 102, 'second:102')
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=7)
    slow_started, release, others_done, stop = (asyncio.Event() for _ in range(4))
    active, sent = Counter(), Counter()

    class Sender:
        async def send(self, chat, *_):
            active[chat] += 1
            assert active[chat] == 1
            if chat == 101:
                slow_started.set()
                await release.wait()
            else:
                await asyncio.sleep(0.001)
            active[chat] -= 1
            sent[chat] += 1
            if sum(sent.values()) == 12 and not sent[101]:
                others_done.set()
            return 500+sent[chat]

    task = asyncio.create_task(send_loop(pool, lambda _:Sender(), stop, poll=0.005))
    try:
        await asyncio.wait_for(slow_started.wait(), 10)
        await asyncio.wait_for(others_done.wait(), 10)
        assert sent[101] == 0 and sent[102] == 2
        stop.set()
        release.set()
        await asyncio.wait_for(task, 10)
        assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE status='sent'") == 13
        assert await c.fetchval('SELECT max(attempt_count) FROM delivery_log') == 1
        assert not await due_jobs(c, [], 20)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_parallel_senders_share_global_rate_limit(db):
    import asyncpg
    c, settings, _ = db
    pool = await asyncpg.create_pool(settings.database_url, min_size=4, max_size=4)

    async def reserve(chat):
        async with pool.acquire() as connection:
            await wait_send_slot(connection, chat)
            return await connection.fetchval("SELECT next_allowed FROM telegram_rate_limits WHERE key=$1", f'chat:{chat}')

    try:
        slots = sorted(await asyncio.gather(*(reserve(chat) for chat in range(101, 105))))
        assert all((right-left).total_seconds() >= 0.049 for left, right in pairwise(slots))
        assert await c.fetchval('SELECT count(*) FROM telegram_rate_limits') == 5
    finally:
        await pool.close()
