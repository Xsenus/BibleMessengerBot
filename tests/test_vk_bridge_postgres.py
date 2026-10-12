"""ВКонтакте целиком: событие → общие обработчики → очередь доставки → отправка через имитацию API (реальная БД)."""
from __future__ import annotations

import os
from urllib.parse import parse_qs

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.services.platform_sender import PlatformTransports
from app.vkbot.bridge import VkBotBridge
from app.vkbot.client import VkClient
from app.vkbot.config import VkSettings
from app.vkbot.dispatcher import VkDispatcher
from app.vkbot.events import parse_event
from app.worker.delivery import process_delivery
from tests.test_postgres_integration import db as db
from tests.test_postgres_integration import load_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]
GROUP = 555


@pytest_asyncio.fixture
async def vk(db, monkeypatch):
    connection, settings, path = db
    monkeypatch.setenv('AUDIO_ENABLED', 'false')
    monkeypatch.setenv('VK_BOT_TOKEN', 't' * 30)
    monkeypatch.setenv('VK_GROUP_ID', str(GROUP))
    await load_fixture(connection, path)
    await load_fixture(connection, path, language='rus')
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=8)
    sent: list[dict] = []

    def handle(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode(errors='ignore')).items()}
        method = request.url.path.rsplit('/', 1)[-1]
        if method == 'users.get':
            return httpx.Response(200, json={'response': [{'id': 101, 'first_name': 'Иван', 'last_name': 'П', 'screen_name': 'ivan'}]})
        if method == 'messages.send':
            sent.append(form)
            return httpx.Response(200, json={'response': [{'peer_id': int(form['peer_ids']), 'conversation_message_id': len(sent)}]})
        if method == 'messages.sendMessageEventAnswer':
            return httpx.Response(200, json={'response': 1})
        if method.endswith('getMessagesUploadServer'):
            return httpx.Response(200, json={'response': {'upload_url': 'https://pu.vk.com/upload'}})
        if request.url.host == 'pu.vk.com':
            return httpx.Response(200, json={'photo': 'p', 'server': 1, 'hash': 'h'})
        if method.endswith('saveMessagesPhoto'):
            return httpx.Response(200, json={'response': [{'owner_id': 1, 'id': 2, 'access_key': 'k'}]})
        return httpx.Response(200, json={'response': {}})

    client = VkClient(VkSettings(token='t' * 30, group_id=GROUP), transport=httpx.MockTransport(handle))
    bridge = VkBotBridge(client, pool, {'id': GROUP, 'screen_name': 'club555', 'name': 'Библия'})
    dispatcher = VkDispatcher(bridge, pool, settings)
    transports = PlatformTransports(None, settings)
    transports.vk_client, transports.vk_group_id = client, GROUP
    try:
        yield connection, dispatcher, transports, sent
    finally:
        await transports.close()
        await pool.close()


def message(text, number):
    return {'type': 'message_new', 'event_id': f'evt{number}', 'object': {'message': {
        'id': number, 'peer_id': 101, 'from_id': 101, 'conversation_message_id': number, 'date': 1700000000, 'text': text}}}


async def drain(connection, transports):
    for _ in range(50):
        identifier = await connection.fetchval("SELECT id FROM delivery_log WHERE status='pending' ORDER BY id LIMIT 1")
        if identifier is None:
            return
        result = await process_delivery(connection, identifier, transports.sender(connection))
        assert result in {'partial', 'sent'}, (result, await connection.fetchrow('SELECT error_code,error_message FROM delivery_log WHERE id=$1', identifier))
    raise AssertionError('VK queue did not drain')


async def test_start_and_random_reach_vk_with_keyboard_and_checkpoint(vk):
    c, dispatcher, transports, sent = vk
    for number, text in enumerate(['/start', '/random'], 1):
        event = message(text, number)
        key, _, _ = parse_event(event)
        await dispatcher.dispatch(event, key)
    assert not sent                                                    # ответы сначала попадают в очередь, не прямо в API
    await drain(c, transports)
    assert sent and all(item['peer_ids'] == '101' for item in sent)
    assert any('keyboard' in item for item in sent)
    chat = await c.fetchrow("SELECT * FROM telegram_chats WHERE platform='vk'")
    assert chat and chat['chat_type'] == 'private'
    card = await c.fetchrow('SELECT * FROM reading_cards')
    assert card['platform'] == 'vk' and card['telegram_message_id'] > 0
    assert await c.fetchval("SELECT count(*) FROM telegram_chats WHERE platform='telegram' AND telegram_chat_id=101") == 0


async def test_repeated_event_is_not_processed_twice_by_inbox(vk):
    c, *_ = vk
    from app.vkbot.events import ingest
    event = message('/today', 7)
    assert await ingest(c, event) is True
    assert await ingest(c, event) is False
    assert await c.fetchval('SELECT count(*) FROM vk_inbox') == 1


# ----- Устойчивость: Long Poll, очередь событий, события разных типов -----------------------------------

class FakeClient:
    """Имитация VkClient для Long Poll: отдаёт заранее заданные ответы."""
    def __init__(self, bodies, server=None):
        self.bodies, self.fresh = list(bodies), server or {'server': 'https://lp.vk.com/x', 'key': 'new', 'ts': '100'}
        self.fetched = 0

    async def poll(self, server):
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return body

    async def long_poll_server(self):
        self.fetched += 1
        if isinstance(self.fresh, Exception):
            raise self.fresh
        return dict(self.fresh)


async def test_poll_once_stores_events_then_advances_ts_and_dedups(vk, monkeypatch):
    from app.vkbot import main as vk_main
    c, dispatcher, *_ = vk
    monkeypatch.setattr(vk_main.asyncio, 'sleep', lambda _s: _noop())
    event = message('/today', 21)
    client = FakeClient([{'ts': '6', 'updates': [event, event, {'bad': 1}]}])
    server = await vk_main.poll_once(client, dispatcher.pool, {'server': 's', 'key': 'k', 'ts': '5'})
    assert server['ts'] == '6'
    assert await c.fetchval('SELECT count(*) FROM vk_inbox') == 1          # дубль и мусор не создают строк


async def _noop():
    return None


async def test_poll_once_failures_keep_position_and_never_raise(vk, monkeypatch):
    from app.vkbot import main as vk_main
    from app.vkbot.client import VkAPIError
    _, dispatcher, *_ = vk
    monkeypatch.setattr(vk_main.asyncio, 'sleep', lambda _s: _noop())
    start = {'server': 's', 'key': 'k', 'ts': '5'}
    assert (await vk_main.poll_once(FakeClient([VkAPIError(0)]), dispatcher.pool, start)) == start
    assert (await vk_main.poll_once(FakeClient([{'failed': 1, 'ts': '9'}]), dispatcher.pool, start))['ts'] == '9'
    client = FakeClient([{'failed': 2}])
    assert (await vk_main.poll_once(client, dispatcher.pool, start))['key'] == 'new' and client.fetched == 1
    broken = FakeClient([{'failed': 3}], server=VkAPIError(0))
    assert (await vk_main.poll_once(broken, dispatcher.pool, start)) == start          # получить сервер не удалось — повтор позже


async def test_db_failure_while_storing_does_not_advance_ts(vk, monkeypatch):
    from app.vkbot import main as vk_main
    _, dispatcher, *_ = vk
    monkeypatch.setattr(vk_main.asyncio, 'sleep', lambda _s: _noop())

    async def explode(*_a, **_k):
        raise RuntimeError('db down')
    monkeypatch.setattr(vk_main, 'ingest', explode)
    start = {'server': 's', 'key': 'k', 'ts': '5'}
    assert (await vk_main.poll_once(FakeClient([{'ts': '8', 'updates': [message('/x', 30)]}]), dispatcher.pool, start))['ts'] == '5'


async def test_unexpected_dispatcher_failure_marks_event_uncertain_without_retry(vk):
    from app.vkbot.events import ingest
    from app.vkbot.inbox import process, recover
    c, dispatcher, *_ = vk

    class Boom:
        calls = 0

        async def dispatch(self, event, key):
            Boom.calls += 1
            raise RuntimeError('boom')
    await ingest(c, message('/today', 40))
    identifier = await c.fetchval('SELECT id FROM vk_inbox')
    assert await process(dispatcher.pool, Boom(), identifier) == 'uncertain'
    assert await process(dispatcher.pool, Boom(), identifier) == 'stale' and Boom.calls == 1
    await c.execute("UPDATE vk_inbox SET status='processing'")
    await recover(c)
    assert await c.fetchval('SELECT status FROM vk_inbox') == 'uncertain'


async def test_events_in_one_chat_are_taken_in_order(vk):
    from app.vkbot.events import ingest
    from app.vkbot.inbox import due_events
    c, *_ = vk
    for n in (50, 51):
        await ingest(c, message('/today', n))
    first = await due_events(c)
    assert len(first) == 1
    await c.execute("UPDATE vk_inbox SET status='processing' WHERE id=$1", first[0]['id'])
    assert await due_events(c) == []                                       # пока идёт первое событие, второе ждёт


async def test_message_deny_and_allow_toggle_chat_and_subscriptions(vk):
    c, dispatcher, transports, sent = vk
    event = message('/start', 60)
    await dispatcher.dispatch(event, parse_event(event)[0])
    deny = {'type': 'message_deny', 'event_id': 'd1', 'object': {'user_id': 101}}
    await dispatcher.dispatch(deny, parse_event(deny)[0])
    assert await c.fetchval("SELECT is_active FROM telegram_chats WHERE platform='vk'") is False
    allow = {'type': 'message_allow', 'event_id': 'a1', 'object': {'user_id': 101}}
    await dispatcher.dispatch(allow, parse_event(allow)[0])
    assert await c.fetchval("SELECT is_active FROM telegram_chats WHERE platform='vk'") is True


async def test_wall_post_and_group_chat_text_feed_sources_only_when_enabled(vk):
    from app.services import chat_sources
    c, dispatcher, *_ = vk
    wall = {'type': 'wall_post_new', 'event_id': 'w1', 'object': {'owner_id': -GROUP, 'id': 5, 'text': 'Новость сообщества', 'date': 1700000000}}
    await dispatcher.dispatch(wall, parse_event(wall)[0])
    source = await c.fetchrow("SELECT * FROM content_sources WHERE platform='vk'")
    assert source and source['is_enabled'] is False and await c.fetchval('SELECT count(*) FROM source_posts') == 0
    await c.execute("UPDATE content_sources SET is_enabled=true,mode='capture' WHERE id=$1", source['id'])
    await dispatcher.dispatch(wall, parse_event(wall)[0])
    await dispatcher.dispatch(wall, parse_event(wall)[0])                  # повтор того же поста
    assert await c.fetchval('SELECT count(*) FROM source_posts') == 1
    assert chat_sources  # модуль доступен


async def test_unknown_or_forged_callbacks_are_answered_not_executed(vk):
    c, dispatcher, transports, sent = vk
    start = message('/start', 70)
    await dispatcher.dispatch(start, parse_event(start)[0])
    for number, data in enumerate(['vkcmd:/admin_evil', 'v1:garbage', 'zzz'], 1):
        event = {'type': 'message_event', 'event_id': f'e{number}', 'object': {
            'user_id': 101, 'peer_id': 101, 'event_id': f'e{number}', 'conversation_message_id': 1, 'payload': {'d': data}}}
        try:
            await dispatcher.dispatch(event, parse_event(event)[0])
        except Exception as error:                                         # допустима только ожидаемая ошибка пользователя
            from app.services.errors import UserError
            assert isinstance(error, UserError)
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='broadcast'") == 0


async def test_vk_owner_gets_admin_menu_and_broadcast_reaches_vk_and_telegram(vk):
    from app.services import fanout
    from tests.test_postgres_integration import create_destination
    c, dispatcher, transports, sent = vk
    start = message('/start', 80)
    await dispatcher.dispatch(start, parse_event(start)[0])
    await drain(c, transports)
    user = await c.fetchval("SELECT telegram_user_id FROM telegram_users WHERE platform='vk'")
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=$1', user)
    before = len(sent)
    admin = message('/admin', 81)
    await dispatcher.dispatch(admin, parse_event(admin)[0])
    await drain(c, transports)
    assert len(sent) > before and 'keyboard' in sent[-1]                      # меню администратора с кнопками
    other = await create_destination(c, 909)                                  # второй получатель — Telegram
    counts = await fanout.enqueue(c, key='test:b', text='Всем!', mode='announcement', audience={})
    assert counts['recipients'] >= 2 and counts['queued'] >= 2
    platforms = {r['platform'] for r in await c.fetch(
        "SELECT ch.platform FROM delivery_log d JOIN telegram_chats ch ON ch.telegram_chat_id=d.telegram_chat_id WHERE d.payload_key='test:b'")}
    assert {'vk', 'telegram'} <= platforms and other
    vk_rows = await c.fetch("SELECT id FROM delivery_log d JOIN telegram_chats ch USING(telegram_chat_id) WHERE d.payload_key='test:b' AND ch.platform='vk'")
    before = len(sent)
    for row in vk_rows:
        assert await __import__('app.worker.delivery', fromlist=['x']).process_delivery(c, row['id'], transports.sender(c)) == 'sent'
    assert len(sent) == before + len(vk_rows) and 'Всем!' in sent[-1]['message']
