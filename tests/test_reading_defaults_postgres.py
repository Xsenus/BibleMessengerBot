"""Регрессии расписания и кнопок приглашения на настоящей PostgreSQL."""
import json
import os
from datetime import UTC, datetime, time, timedelta

import pytest

from app.bot.commands import decode_callback
from app.services.errors import UserError
from app.services.reading_defaults import answer_offer, initialize, prepare_due
from app.services.subscriptions import create_or_update_subscription, delete_subscriptions, set_enabled
from tests.test_postgres_integration import Sender, create_destination, drain, load_fixture
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


async def test_backfill_migrates_old_time_without_second_delivery_today(db):
    c, _, tmp_path = db
    edition, _, _ = await load_fixture(c, tmp_path)
    await create_destination(c)
    await create_destination(c, 102)
    await c.execute("UPDATE telegram_chats SET platform='max' WHERE telegram_chat_id=102")
    await create_destination(c, -100)
    for chat, clock in ((101, time(9)), (102, time(15, 30))):
        await create_or_update_subscription(c, chat_id=chat, created_by=101, translation_id=edition['id'],
            mode='verse_of_day', send_time=clock, timezone_name='UTC')
    await c.execute("""INSERT INTO delivery_log(telegram_chat_id,mode,payload_key,scheduled_for,status,chunks,progress)
        VALUES(101,'verse_of_day','legacy-versemorning','2026-01-02 09:00+00','sent','[]'::jsonb,'{}'::jsonb)""")

    now = datetime(2026, 1, 2, 10, tzinfo=UTC)
    await prepare_due(c, now=now)
    await prepare_due(c, now=now)
    existing = await c.fetch('SELECT telegram_chat_id,mode,send_time,next_run_at,is_enabled FROM subscriptions')
    assert len(existing) == 6 and all(r['is_enabled'] for r in existing)
    by_chat = {(r['telegram_chat_id'], r['mode']): r for r in existing}
    assert by_chat[101, 'verse_of_day']['send_time'] == time(14)
    assert by_chat[101, 'verse_of_day']['next_run_at'] == datetime(2026, 1, 3, 14, tzinfo=UTC)
    assert by_chat[102, 'verse_of_day']['send_time'] == time(15, 30)
    for chat in (101, 102):
        assert by_chat[chat, 'morning_verse']['send_time'] == time(9, 38)
        assert by_chat[chat, 'evening_verse']['send_time'] == time(21, 13)


@pytest.mark.parametrize('platform', ['telegram', 'max'])
async def test_one_invitation_with_two_buttons_resumes_only_paused_modes(db, platform):
    c, _, tmp_path = db
    await load_fixture(c, tmp_path)
    await create_destination(c)
    chat = await c.fetchrow('UPDATE telegram_chats SET platform=$1 WHERE telegram_chat_id=101 RETURNING *', platform)
    await initialize(c, 101)
    await delete_subscriptions(c, 101, 'evening_verse')
    await set_enabled(c, 101, False, 'verse_of_day')
    due = await c.fetchval('SELECT offer_at FROM reading_schedule_preferences WHERE telegram_chat_id=101')
    assert due.time() == time(20)
    await initialize(c, 101)

    now = datetime.now(UTC)
    await c.execute('UPDATE reading_schedule_preferences SET offer_at=$1 WHERE telegram_chat_id=101', now-timedelta(seconds=1))
    await prepare_due(c, now=now)
    await prepare_due(c, now=now)
    offers = await c.fetch("SELECT * FROM delivery_log WHERE mode='reading_offer'")
    assert len(offers) == 1
    offer = offers[0]
    chunk = json.loads(offer['chunks'])[0]
    assert 'SYNTHETIC NOT SCRIPTURE' in chunk['text']
    if platform == 'telegram':
        buttons = [row[0]['callback_data'] for row in chunk['inline_keyboard']['inline_keyboard']]
    else:
        buttons = [row[0]['payload'] for row in chunk['max_keyboard']['payload']['buttons']]
    assert [decode_callback(button) for button in buttons] == [
        ('readingon', 101, str(offer['id'])), ('readingno', 101, str(offer['id']))]
    await drain(c, offer['id'], Sender())

    with pytest.raises(UserError, match='invalid'):
        await answer_offer(c, chat, 101, 99, offer['id'], True)
    await answer_offer(c, chat, 101, 1, offer['id'], True)
    subs = await c.fetch('SELECT mode,is_enabled FROM subscriptions')
    assert dict(subs) == {'morning_verse': True, 'verse_of_day': True}
    with pytest.raises(UserError, match='invalid'):
        await answer_offer(c, chat, 101, 1, offer['id'], True)


async def test_mute_survives_another_pause(db):
    c, _, tmp_path = db
    await load_fixture(c, tmp_path)
    chat = await create_destination(c)
    await initialize(c, 101)
    await set_enabled(c, 101, False, 'verse_of_day')
    now = datetime.now(UTC)
    await c.execute('UPDATE reading_schedule_preferences SET offer_at=$1', now-timedelta(seconds=1))
    await prepare_due(c, now=now)
    offer_id = await c.fetchval("SELECT id FROM delivery_log WHERE mode='reading_offer'")
    await drain(c, offer_id, Sender())
    await answer_offer(c, chat, 101, 1, offer_id, False)

    await set_enabled(c, 101, False, 'morning_verse')
    await prepare_due(c, now=now+timedelta(days=2))
    prefs = await c.fetchrow('SELECT * FROM reading_schedule_preferences WHERE telegram_chat_id=101')
    assert prefs['offers_muted'] and prefs['offer_at'] is None
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='reading_offer'") == 1
