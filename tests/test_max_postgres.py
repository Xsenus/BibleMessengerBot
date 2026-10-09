"""Real DB tests of platform isolation and durable MAX edit/inbox identities."""
from __future__ import annotations

import json
import os

import pytest

from app.maxbot.events import ingest
from app.maxbot.identities import external, message_external, register, remember_message
from app.services.accounts import upsert_chat, upsert_user
from app.services.errors import UserError
from tests.test_max_events import fixture_event
from tests.test_postgres_integration import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1',
                  reason='Real PostgreSQL tests require RUN_DB_TESTS=1')]


async def test_equal_external_ids_do_not_touch_telegram_preferences(db):
    connection, _, _ = db
    await upsert_user(connection, user_id=101, username='telegram-fixture', first_name='TG',
                      last_name=None, language_code='en', default_timezone='Europe/Moscow')
    await upsert_chat(connection, chat_id=101, chat_type='private', title=None, username=None,
                      registered_by=101, default_timezone='Europe/Moscow', ui_language='en')
    actor, chat = await register(connection, user={'user_id': 101, 'first_name': 'MAX'},
                                 external_chat_id=303, chat_type='private', timezone='UTC')
    assert actor == chat and chat != 101
    tg = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    assert tg['platform'] == 'telegram' and tg['timezone'] == 'Europe/Moscow' and tg['ui_language'] == 'en'
    assert await external(connection, 'user', actor) == 101
    assert await external(connection, 'chat', chat) == 303
    await connection.execute("UPDATE telegram_chats SET timezone='Asia/Novosibirsk',ui_language='en' WHERE telegram_chat_id=$1", chat)
    assert await register(connection, user={'user_id': 101, 'first_name': 'MAX updated'},
                          external_chat_id=303, chat_type='private', timezone='UTC') == (actor, chat)
    updated = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', chat)
    assert updated['timezone'] == 'Asia/Novosibirsk' and updated['ui_language'] == 'en'


async def test_private_dialog_cannot_be_rebound_to_another_user(db):
    connection, _, _ = db
    await register(connection, user={'user_id': 101}, external_chat_id=303,
                   chat_type='private', timezone='UTC')
    with pytest.raises(UserError):
        await register(connection, user={'user_id': 102}, external_chat_id=303,
                       chat_type='private', timezone='UTC')
    assert not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM platform_identities WHERE kind='user' AND external_id=102)")


async def test_string_message_checkpoint_scoped_to_bot_and_chat(db):
    connection, _, _ = db
    _, chat = await register(connection, user={'user_id': 101}, external_chat_id=303,
                             chat_type='private', timezone='UTC')
    _, other = await register(connection, user={'user_id': 102}, external_chat_id=304,
                              chat_type='private', timezone='UTC')
    identifier = await remember_message(connection, 900, chat, 'mid.fixture')
    assert identifier > 0
    assert await remember_message(connection, 900, chat, 'mid.fixture') == identifier
    assert await message_external(connection, 900, chat, identifier) == 'mid.fixture'
    for bot, destination in [(900, other), (901, chat)]:
        with pytest.raises(UserError):
            await message_external(connection, bot, destination, identifier)


async def test_duplicate_webhook_has_one_durable_job(db):
    connection, _, _ = db
    event = fixture_event()
    assert await ingest(connection, json.dumps(event).encode())
    event['timestamp'] += 1
    assert not await ingest(connection, json.dumps(event).encode())
    assert await connection.fetchval('SELECT count(*) FROM max_inbox') == 1
    row = await connection.fetchrow('SELECT * FROM max_inbox')
    assert row['status'] == 'pending' and row['attempts'] == 0
