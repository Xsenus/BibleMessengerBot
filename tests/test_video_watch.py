"""Слежение за видеоканалами: разбор лент, первый опрос без рассылки, дедупликация, сбои и права."""
import os

import httpx
import pytest

from app.services import video_watch as vw
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

CHANNEL = 'UC' + 'a' * 22


def atom(*ids):
    entries = ''.join(f'<entry><yt:videoId>{i}</yt:videoId><title>T {i}</title><published>2099-01-0{n + 1}T10:00:00+00:00</published></entry>'
                      for n, i in enumerate(ids))
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{entries}</feed>').encode()


def test_parse_handle():
    assert vw.parse_handle('youtube', f'https://www.youtube.com/channel/{CHANNEL}') == CHANNEL
    assert vw.parse_handle('rutube', 'https://rutube.ru/channel/12345/') == '12345'
    assert vw.parse_handle('vk', 'club123') == '-123'
    assert vw.parse_handle('vk', '-77') == '-77'
    with pytest.raises(UserError):
        vw.parse_handle('youtube', 'not a channel')


def test_parse_youtube_and_rejects_entities():
    items = vw.parse_youtube(atom('abcdefghijk', 'bbbbbbbbbbb'))
    assert [i['id'] for i in items] == ['abcdefghijk', 'bbbbbbbbbbb'] and items[0]['url'].endswith('abcdefghijk')
    with pytest.raises(ValueError):
        vw.parse_youtube(b'<!DOCTYPE x [<!ENTITY a "b">]><feed/>')


def test_parse_rutube_and_vk():
    vid = 'a' * 32
    assert vw.parse_rutube({'results': [{'id': vid, 'title': 't', 'created_ts': '2026-01-01T00:00:00'}, {'id': 'bad'}]})[0]['url'].endswith(f'{vid}/')
    assert vw.parse_vk({'items': [{'id': 5, 'owner_id': -9, 'title': 'x', 'date': 1700000000}]})[0]['url'] == 'https://vk.com/video-9_5'


def test_render_escapes_title():
    text = vw.render({'template': '', 'title': 'К', 'handle': 'h'}, {'title': '<b>x</b>', 'url': 'https://u'})
    assert '&lt;b&gt;' in text and 'https://u' in text


pytestmark_db = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


def client_for(state):
    def handler(request):
        if state.get('fail'):
            return httpx.Response(503)
        return httpx.Response(200, content=atom(*state['ids']))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytestmark_db
@pytest.mark.asyncio
async def test_baseline_then_new_video_notifies_once(db):
    c = db[0]
    await create_destination(c, 101)
    feed = await vw.add(c, 'youtube', CHANNEL, notify=True)
    state = {'ids': ['aaaaaaaaaaa']}
    async with client_for(state) as http:
        assert await vw.poll_feed(c, feed, http) == 0                       # первый опрос — только запоминание
        assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='video_notice'") == 0
        feed = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
        state['ids'] = ['aaaaaaaaaaa', 'bbbbbbbbbbb']
        assert await vw.poll_feed(c, feed, http) == 1
        assert await vw.poll_feed(c, feed, http) == 0                       # повтор не дублирует
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='video_notice'") == 1


@pytestmark_db
@pytest.mark.asyncio
async def test_silent_feed_records_without_notifying_and_failure_backs_off(db):
    c = db[0]
    await create_destination(c, 101)
    feed = await vw.add(c, 'youtube', CHANNEL)
    state = {'ids': ['aaaaaaaaaaa']}
    async with client_for(state) as http:
        await vw.poll_feed(c, feed, http)
        feed = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
        state['ids'] = ['aaaaaaaaaaa', 'bbbbbbbbbbb']
        assert await vw.poll_feed(c, feed, http) == 0
        assert await c.fetchval("SELECT status FROM video_items WHERE external_id='bbbbbbbbbbb'") == 'skipped'
        state['fail'] = True
        feed = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
        await vw.poll_feed(c, feed, http)
    row = await c.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
    assert row['error_count'] == 1 and row['last_error'] == 'HTTPStatusError'
    assert await c.fetchval('SELECT next_check_at>now()+interval \'30 minutes\' FROM video_feeds WHERE id=$1', feed['id'])


@pytestmark_db
@pytest.mark.asyncio
async def test_command_requires_admin_and_toggles(db):
    c = db[0]
    chat = dict(await create_destination(c, 101))
    await create_destination(c, 202)
    await c.execute("UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101")
    with pytest.raises(UserError):
        await vw.command(c, 202, chat, ['add', 'youtube', CHANNEL])
    await vw.command(c, 101, chat, ['add', 'youtube', CHANNEL, 'notify'])
    feed_id = await c.fetchval('SELECT id FROM video_feeds')
    await vw.command(c, 101, chat, ['silent', str(feed_id)])
    assert await c.fetchval('SELECT notify FROM video_feeds') is False
    text, _ = await vw.section(c, 101, chat, f'vt{feed_id}')
    assert await c.fetchval('SELECT is_enabled FROM video_feeds') is False and 'Video channels' in text
