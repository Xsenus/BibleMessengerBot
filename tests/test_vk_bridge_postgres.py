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
