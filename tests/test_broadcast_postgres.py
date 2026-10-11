"""Рассылка отрывка всем: только владелец, подтверждение, очередь на каждого получателя."""
import json
import os

import pytest

from app.services import broadcast
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')
REFS = [dict(book='GEN', chapter=1, first=1, last=1)]


async def setup(db):
    c, _, tmp = db
    edition, _, _ = await load_fixture(c, tmp)
    owner = await create_destination(c, 101)            # владелец (личный чат 101)
    await create_destination(c, 102)                    # обычный пользователь
    await create_destination(c, -100500, 'ru')          # канал
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')
    await c.execute('UPDATE telegram_chats SET default_translation_id=$1', edition['id'])
    return c, edition, owner


@pytest.mark.asyncio
async def test_only_owner_gets_offer(db):
    c, edition, _ = await setup(db)
    assert await broadcast.offer(c, 101, REFS, edition['id']) is not None
    assert broadcast.take_offer() is not None and broadcast.take_offer() is None
    await c.execute('INSERT INTO telegram_users(telegram_user_id,timezone) VALUES(555,\'UTC\') ON CONFLICT DO NOTHING')
    assert await broadcast.offer(c, 555, REFS, edition['id']) is None
    assert broadcast.take_offer() is None
    with pytest.raises(UserError):
        await broadcast.confirmation(c, 1, 555, {'ui_language': 'ru', 'telegram_chat_id': 555})


@pytest.mark.asyncio
async def test_send_queues_each_destination_once(db):
    c, edition, owner = await setup(db)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,timezone,is_blocked) VALUES(102,'UTC',true) ON CONFLICT(telegram_user_id) DO UPDATE SET is_blocked=true")
    draft = await broadcast.offer(c, 101, REFS, edition['id'])
    text, markup = await broadcast.confirmation(c, draft, 101, owner)
    assert 'Получателей: 2' in text or 'Recipients: 2' in text and len(markup.inline_keyboard[0]) == 2   # заблокированный не считается
    counts = await broadcast.send(c, draft, 101)
    assert counts == {'recipients': 2, 'queued': 2, 'skipped': 0}
    rows = await c.fetch("SELECT telegram_chat_id,mode,payload_key,chunks,status FROM delivery_log WHERE mode='broadcast' ORDER BY 1")
    assert [r['telegram_chat_id'] for r in rows] == [-100500, 101]
    assert all(r['payload_key'] == f'broadcast:{draft}' and r['status'] == 'pending' for r in rows)
    assert 'SYNTHETIC' in json.dumps(rows[0]['chunks'])
    with pytest.raises(UserError):                          # повторная отправка не дублирует
        await broadcast.send(c, draft, 101)
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='broadcast'") == 2
    assert await c.fetchval('SELECT status FROM broadcasts WHERE id=$1', draft) == 'done'


@pytest.mark.asyncio
async def test_skips_destination_without_passage_and_cancel(db):
    c, edition, owner = await setup(db)
    draft = await broadcast.offer(c, 101, [dict(book='GEN', chapter=99, first=1, last=1)], edition['id'])
    counts = await broadcast.send(c, draft, 101)
    assert counts['queued'] == 0 and counts['skipped'] == 3
    other = await broadcast.offer(c, 101, REFS, edition['id'])
    await broadcast.cancel(c, other, 101)
    with pytest.raises(UserError):
        await broadcast.send(c, other, 101)


@pytest.mark.asyncio
async def test_queued_broadcast_is_delivered_even_after_chat_settings_change(db):
    from app.worker.delivery import process_delivery
    c, edition, _ = await setup(db)
    draft = await broadcast.offer(c, 101, REFS, edition['id'])
    await broadcast.send(c, draft, 101)
    await c.execute('UPDATE telegram_chats SET revision=revision+1 WHERE telegram_chat_id=102')  # настройки сменились
    sent = []

    class Sender:
        async def send(self, chat_id, part, thread_id=None, *, reply_markup=None):
            sent.append(chat_id)
            return len(sent)

    for row in await c.fetch("SELECT id FROM delivery_log WHERE mode='broadcast'"):
        while (await process_delivery(c, row['id'], Sender())) in {'partial', 'sent'}:
            pass
    assert sorted(sent) == [-100500, 101, 102]
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='broadcast' AND status='sent'") == 3
