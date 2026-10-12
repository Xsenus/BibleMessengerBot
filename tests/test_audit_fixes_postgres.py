"""Исправления по результатам аудита: расписания, зависшие запуски, публикация постов, опрос видео, проверка адресов."""
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import announcements as a
from app.services import chat_sources as cs
from app.services import fanout
from app.services import video_watch as vw
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

db_required = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


def test_weekly_accepts_several_days_and_pipes_in_text():
    schedule = a.parse_when('weekly sat,sun 10:00 Europe/Moscow')
    assert schedule['days_of_week'] == [6, 7] and schedule['timezone'] == 'Europe/Moscow'
    assert a.parse_when('weekly mon, wed, fri 08:30')['days_of_week'] == [1, 3, 5]
    assert a._split_command('now | Заголовок | Текст | с чертой') == ['now', 'Заголовок', 'Текст | с чертой']
    assert a.parse_when('daily 09:00 america/port-au-prince')['timezone'] == 'America/Port-au-Prince'


def test_audience_ids_are_bounded():
    assert fanout.normalize_audience({'chat_ids': [1, 2**70, -2**70, 'x']}) == {'chat_ids': [1]}


@pytest.mark.asyncio
async def test_public_host_rejects_internal_names_and_resolved_private_addresses(monkeypatch):
    assert not cs.safe_image_url('https://postgres/x.png') and cs.safe_image_url('https://example.com/x.png')
    monkeypatch.setattr(cs, 'DNS_CHECK', True)

    class Loop:
        def __init__(self, address):
            self.address = address

        async def getaddrinfo(self, *_a, **_k):
            return [(2, 1, 6, '', (self.address, 443))]
    import asyncio
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: Loop('10.0.0.5'))
    assert not await cs.public_host('https://rebind.example.com/x.png')
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: Loop('93.184.216.34'))
    assert await cs.public_host('https://real.example.com/x.png')


async def owner_chat(c):
    await create_destination(c, 101)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone,first_name) VALUES(101,'telegram','UTC','A') ON CONFLICT DO NOTHING")
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')
    return dict(await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101'))


@db_required
@pytest.mark.asyncio
async def test_resume_keeps_upcoming_occurrence(db):
    c = db[0]
    chat = await owner_chat(c)
    await a.command(c, 101, chat, 'daily 09:00 | T | Текст')
    ann = await c.fetchrow('SELECT * FROM announcements')
    soon = datetime.now(UTC) + timedelta(hours=1)
    await c.execute('UPDATE announcements SET next_run_at=$2 WHERE id=$1', ann['id'], soon)
    await a.set_status(c, ann['id'], 'paused')
    await a.set_status(c, ann['id'], 'active')
    nxt = await c.fetchval('SELECT next_run_at FROM announcements')
    assert nxt < soon + timedelta(hours=2) and nxt > datetime.now(UTC)       # ближайший запуск не потерян: не «завтра через сутки после него»


@db_required
@pytest.mark.asyncio
async def test_failing_stuck_run_does_not_block_other_announcements(db, monkeypatch):
    c = db[0]
    chat = await owner_chat(c)
    await a.command(c, 101, chat, 'now | Один | Текст')
    first = await c.fetchrow('SELECT * FROM announcements')
    run = await c.fetchval("INSERT INTO announcement_runs(announcement_id,scheduled_for,status,created_at) VALUES($1,now()-interval '1 hour','sending',now()-interval '1 hour') RETURNING id", first['id'])
    original = fanout.enqueue

    async def explode(connection, **kwargs):
        if kwargs['key'] == f'announcement:{run}':
            raise RuntimeError('bad audience')
        return await original(connection, **kwargs)
    monkeypatch.setattr(fanout, 'enqueue', explode)
    await a.command(c, 101, chat, 'now | Два | Текст два')
    assert await a.run_due(c) >= 1                                           # второе объявление ушло, несмотря на «ядовитый» запуск
    for _ in range(5):
        await a.run_due(c)
    assert await c.fetchval('SELECT status FROM announcement_runs WHERE id=$1', run) == 'failed'


@db_required
@pytest.mark.asyncio
async def test_publish_failure_returns_post_to_new(db, monkeypatch):
    c = db[0]
    await owner_chat(c)
    source = await cs.ensure_source(c, 'telegram', -100500, 'Канал')
    await c.execute("UPDATE content_sources SET is_enabled=true,mode='capture',min_length=1 WHERE id=$1", source['id'])
    post = await cs.ingest_post(c, 'telegram', -100500, '1', 'Текст')
    monkeypatch.setattr(fanout, 'enqueue', AsyncMock(side_effect=RuntimeError('db down')))
    with pytest.raises(RuntimeError):
        await cs.publish(c, post)
    assert await c.fetchval('SELECT status FROM source_posts WHERE id=$1', post) == 'new'
    monkeypatch.undo()
    assert (await cs.publish(c, post))['queued'] >= 1


@db_required
@pytest.mark.asyncio
async def test_video_feed_db_error_is_recorded_not_retried_hot(db):
    c = db[0]
    await owner_chat(c)
    feed = await vw.add(c, 'youtube', 'UC' + 'z' * 22)
    entry = '<entry><yt:videoId>abcdefghijk</yt:videoId><title>bad \x00 title</title><published>2026-01-01T00:00:00+00:00</published></entry>'
    body = f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{entry}</feed>'.replace('\x00', '')
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body.encode())))
    await c.execute('UPDATE video_feeds SET next_check_at=now()')
    assert await vw.run_due(c, http) == 0
    row = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
    assert row['baselined'] and row['next_check_at'] > datetime.now(UTC)
    assert await vw.run_due(c, http) == 0                                    # повторно сразу ничего не берётся
    await http.aclose()
    assert vw.clean('a\x00b') == 'ab'


@db_required
@pytest.mark.asyncio
async def test_old_video_returning_to_feed_window_is_not_announced(db):
    c = db[0]
    await owner_chat(c)
    feed = await vw.add(c, 'youtube', 'UC' + 'y' * 22, notify=True)
    state = {'ids': [('aaaaaaaaaaa', '2026-10-01T00:00:00+00:00')]}

    def handler(request):
        entries = ''.join(f'<entry><yt:videoId>{i}</yt:videoId><title>t</title><published>{p}</published></entry>' for i, p in state['ids'])
        return httpx.Response(200, content=f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{entries}</feed>'.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await vw.poll_feed(c, feed, http)
        feed = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
        state['ids'].append(('oldoldold01', '2019-01-01T00:00:00+00:00'))     # старое видео снова попало в окно ленты
        state['ids'].append(('brandnew001', '2099-01-01T00:00:00+00:00'))
        assert await vw.poll_feed(c, feed, http) == 1
    assert await c.fetchval("SELECT status FROM video_items WHERE external_id='oldoldold01'") == 'skipped'
