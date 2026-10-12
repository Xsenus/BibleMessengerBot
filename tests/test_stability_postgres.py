"""Устойчивость: параллельные запуски объявлений и опросов, обработчики каналов Telegram, повторы без дублей."""
import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import pytest

from app.bot import handlers
from app.services import announcements as a
from app.services import video_watch as vw
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


class PoolOf:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        outer = self

        class Ctx:
            async def __aenter__(self):
                return outer.connection

            async def __aexit__(self, *_):
                return False
        return Ctx()


async def test_parallel_composers_never_duplicate_an_announcement(db):
    c, settings, _ = db
    await create_destination(c, 101)
    await create_destination(c, 202)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone,first_name) VALUES(101,'telegram','UTC','A') ON CONFLICT DO NOTHING")
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')
    chat = dict(await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101'))
    await a.command(c, 101, chat, 'now | T | Текст')
    pool = await asyncpg.create_pool(settings.database_url, min_size=3, max_size=3)
    try:
        async def worker():
            async with pool.acquire() as conn:
                return await a.run_due(conn)
        await asyncio.gather(*(worker() for _ in range(3)))
    finally:
        await pool.close()
    assert await c.fetchval('SELECT count(*) FROM announcement_runs') == 1
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='announcement' AND telegram_chat_id=101") == 1


async def test_long_downtime_does_not_replay_old_runs(db):
    c, *_ = db
    await create_destination(c, 101)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone,first_name) VALUES(101,'telegram','UTC','A') ON CONFLICT DO NOTHING")
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')
    chat = dict(await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101'))
    await a.command(c, 101, chat, 'daily 09:00 | T | Текст')
    await c.execute("UPDATE announcements SET next_run_at=now()-interval '3 days'")
    await a.run_due(c)
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='announcement'") == 0
    assert await c.fetchval("SELECT next_run_at>now() FROM announcements")
    assert await c.fetchval("SELECT status FROM announcement_runs") == 'skipped_late'


async def test_parallel_video_pollers_notify_once(db):
    import httpx
    c, settings, _ = db
    await create_destination(c, 101)
    feed = await vw.add(c, 'youtube', 'UC' + 'b' * 22, notify=True)
    state = {'ids': ['aaaaaaaaaaa']}

    def handler(request):
        entries = ''.join(f'<entry><yt:videoId>{i}</yt:videoId><title>t</title><published>2026-01-01T00:00:00+00:00</published></entry>' for i in state['ids'])
        return httpx.Response(200, content=(f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{entries}</feed>').encode())
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await vw.run_due(c, http)                                              # первый опрос: baseline
    await c.execute('UPDATE video_feeds SET next_check_at=now()')
    state['ids'] = ['aaaaaaaaaaa', 'ccccccccccc']
    pool = await asyncpg.create_pool(settings.database_url, min_size=3, max_size=3)
    try:
        async def poll():
            async with pool.acquire() as conn:
                await vw.run_due(conn, http)
        await asyncio.gather(*(poll() for _ in range(3)))
    finally:
        await pool.close()
        await http.aclose()
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='video_notice'") == 1
    assert feed


def tg_message(chat_id, text, mid, **extra):
    return SimpleNamespace(text=text, caption=None, photo=None, message_id=mid, date=datetime.now(UTC),
                           chat=SimpleNamespace(id=chat_id, title='Канал', type='channel'),
                           from_user=SimpleNamespace(is_bot=False), **extra)


async def test_telegram_channel_post_and_group_handlers(db):
    c, *_ = db
    pool = PoolOf(c)
    bot = SimpleNamespace(download=None)
    await handlers.channel_post_handler(tg_message(-100900, 'Первый', 1), bot, pool)
    assert await c.fetchval("SELECT is_enabled FROM content_sources WHERE external_id=-100900") is False
    assert await c.fetchval('SELECT count(*) FROM source_posts') == 0
    await c.execute("UPDATE content_sources SET is_enabled=true,mode='capture'")
    await handlers.channel_post_handler(tg_message(-100900, 'Второй', 2), bot, pool)
    await handlers.channel_post_handler(tg_message(-100900, 'Второй', 2), bot, pool)     # повтор доставки
    await handlers.group_message_handler(tg_message(-100901, 'Привет группа', 3), pool)
    assert await c.fetchval("SELECT count(*) FROM source_posts WHERE body='Второй'") == 1
    bot_msg = tg_message(-100900, 'От бота', 4)
    bot_msg.from_user.is_bot = True
    await handlers.group_message_handler(bot_msg, pool)
    assert await c.fetchval("SELECT count(*) FROM source_posts WHERE body='От бота'") == 0
