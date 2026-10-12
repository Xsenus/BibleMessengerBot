"""Пакетная рассылка, независимые циклы и их статистика."""
import asyncio
import os
import time
from contextlib import asynccontextmanager

import pytest

from app.services import fanout, loops
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]
BASE = 7_000_000_000_000


async def make_chats(c, n):
    await c.execute('''INSERT INTO telegram_chats(telegram_chat_id,chat_type,title,timezone,platform,ui_language)
        SELECT $1::bigint+g,'channel','c'||g,'UTC',(ARRAY['telegram','max','vk'])[1+g%3],'ru' FROM generate_series(1,$2) g''', BASE, n)


async def test_batch_enqueue_is_fast_idempotent_and_per_platform(db):
    c = db[0]
    await make_chats(c, 2000)
    started = time.perf_counter()
    first = await fanout.enqueue(c, key='perf:1', text='<b>Тест</b> текст', mode='announcement', audience={'chat_types': ['channel']})
    assert time.perf_counter() - started < 5
    assert first == {'recipients': 2000, 'queued': 2000, 'skipped': 0, 'failed': 0}
    again = await fanout.enqueue(c, key='perf:1', text='<b>Тест</b> текст', mode='announcement', audience={'chat_types': ['channel']})
    assert again['queued'] == 2000 and await c.fetchval("SELECT count(*) FROM delivery_log WHERE payload_key='perf:1'") == 2000
    kinds = {r['platform']: r['chunks'] for r in await c.fetch(
        "SELECT ch.platform,d.chunks FROM delivery_log d JOIN telegram_chats ch USING(telegram_chat_id) WHERE d.payload_key='perf:1' LIMIT 3000")}
    assert 'max_text' in str(kinds['max']) and 'max_text' not in str(kinds['telegram'])


async def test_failed_batch_falls_back_to_single_chats(db, monkeypatch):
    c = db[0]
    await make_chats(c, 30)
    state = {'failed': False}

    class Flaky:
        """Обёртка соединения: первая пакетная вставка (unnest) завершается ошибкой."""
        def __getattr__(self, name):
            return getattr(c, name)

        async def execute(self, query, *args, **kwargs):
            if 'unnest' in str(query) and not state['failed']:
                state['failed'] = True
                raise RuntimeError('batch insert failed')
            return await c.execute(query, *args, **kwargs)
    c = db[0]
    result = await fanout.enqueue(Flaky(), key='perf:2', text='текст', mode='announcement', audience={'chat_types': ['channel']})
    assert state['failed'] and result['queued'] == 30 and result['failed'] == 0
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE payload_key='perf:2'") == 30


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


async def test_loops_record_duration_and_errors_and_survive_failures(db):
    c = db[0]
    pool = Pool(c)
    calls = {'n': 0}

    async def step(connection):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('boom')
    task = asyncio.create_task(loops.run_forever(pool, 'demo', step, 0.01))
    for _ in range(300):
        await asyncio.sleep(0.02)
        if calls['n'] >= 1 and await c.fetchval("SELECT runs FROM loop_stats WHERE name='demo'") >= 2:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    row = await c.fetchrow("SELECT * FROM loop_stats WHERE name='demo'")
    assert row['errors'] == 1 and row['runs'] >= 2 and row['last_ok_at'] is not None
