"""Рассылки, приостановленные из-за остановки бота, включаются сами, когда его снова запускают; ручная пауза — нет."""
import os
from dataclasses import replace
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import membership_handler
from app.cli import resume_subscriptions
from app.services.subscriptions import create_or_update_subscription, set_enabled
from tests.test_admin_api_postgres import Pool
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


async def subscribe(c, tmp_path, chat_id=101):
    edition, _, _ = await load_fixture(c, tmp_path)
    await c.execute("UPDATE translations SET audit_status='passed' WHERE id=$1", edition['id'])
    for mode, clock in (('verse_of_day', time(9)), ('evening_verse', time(21, 13))):
        await create_or_update_subscription(c, chat_id=chat_id, created_by=101, translation_id=edition['id'],
                                            mode=mode, send_time=clock, timezone_name='UTC')


def event(status, chat_id=101):
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(new_chat_member=SimpleNamespace(status=status), chat=SimpleNamespace(id=chat_id, type='private'), bot=bot)


async def stale_delivery(c, chat_id=101):
    sub = await c.fetchval("SELECT id FROM subscriptions WHERE telegram_chat_id=$1 AND mode='verse_of_day'", chat_id)
    return await c.fetchval("""INSERT INTO delivery_log(subscription_id,telegram_chat_id,mode,scheduled_for,payload_key,status,chunks,progress)
        VALUES($1,$2,'verse_of_day',now()-interval '5 days','stale','pending','[]'::jsonb,'{}'::jsonb) RETURNING id""", sub, chat_id)


async def test_bot_stopped_then_started_resumes_and_skips_stale(db):
    c, _, tmp_path = db
    await create_destination(c, 101, 'ru')
    await subscribe(c, tmp_path)
    await membership_handler(event('kicked'), Pool(c))
    paused = await c.fetch('SELECT is_enabled,next_run_at,paused_reason FROM subscriptions WHERE telegram_chat_id=101')
    assert [tuple(r) for r in paused] == [(False, None, 'bot_unavailable')] * 2
    assert await c.fetchval("SELECT details->>'cause' FROM operator_events WHERE action='subscriptions_paused'") == 'telegram:kicked'
    stale = await stale_delivery(c)

    started = event('member')
    await membership_handler(started, Pool(c))
    rows = await c.fetch('SELECT is_enabled,next_run_at,paused_reason FROM subscriptions WHERE telegram_chat_id=101')
    assert all(r['is_enabled'] and r['next_run_at'] and r['paused_reason'] is None for r in rows) and len(rows) == 2
    assert await c.fetchval('SELECT is_active FROM telegram_chats WHERE telegram_chat_id=101')
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1', stale) == 'skipped'   # старое не досылается
    started.bot.send_message.assert_awaited_once()
    assert 'Рассылки снова включены' in started.bot.send_message.await_args.args[1]
    assert await c.fetchval("SELECT count(*) FROM operator_events WHERE action='subscriptions_resumed'") == 1

    again = event('member')                                     # повторное событие ничего не включает и не пишет
    await membership_handler(again, Pool(c))
    again.bot.send_message.assert_not_awaited()


async def test_manual_pause_and_pending_review_are_not_resumed(db):
    c, _, tmp_path = db
    await create_destination(c, 101)
    await subscribe(c, tmp_path)
    await set_enabled(c, 101, False, 'evening_verse')           # пауза вручную, до остановки бота
    await membership_handler(event('kicked'), Pool(c))
    started = event('member')
    await membership_handler(started, Pool(c))
    state = dict(await c.fetch('SELECT mode,is_enabled FROM subscriptions WHERE telegram_chat_id=101'))
    assert state == {'verse_of_day': True, 'evening_verse': False}

    await membership_handler(event('kicked'), Pool(c))
    await c.execute("UPDATE delivery_log SET status='uncertain' WHERE id=$1", await stale_delivery(c))
    blocked = event('member')
    await membership_handler(blocked, Pool(c))                  # неопределённая отправка: ждём решения оператора
    assert not await c.fetchval("SELECT is_enabled FROM subscriptions WHERE telegram_chat_id=101 AND mode='verse_of_day'")
    blocked.bot.send_message.assert_not_awaited()


async def test_cli_resume_subscriptions_dry_run_and_all(db):
    c, settings, tmp_path = db
    await create_destination(c, 101)
    await subscribe(c, tmp_path)
    await c.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL')   # как на сервере: причина неизвестна
    stale = await stale_delivery(c)
    settings = replace(settings)

    only_marked = await resume_subscriptions(settings, any_reason=False, apply=True)
    assert only_marked['chats'] == [] and only_marked['resumed'] == 0

    preview = await resume_subscriptions(settings, any_reason=True, apply=False)
    assert preview['applied'] is False and preview['chats'][0]['status'] == 'would_resume'
    assert sorted(preview['chats'][0]['modes']) == ['evening_verse', 'verse_of_day']
    assert not await c.fetchval('SELECT bool_or(is_enabled) FROM subscriptions')     # без --apply ничего не меняется

    done = await resume_subscriptions(settings, any_reason=True, apply=True)
    assert done['resumed'] == 2 and done['chats'][0]['status'] == 'resumed'
    assert await c.fetchval('SELECT bool_and(is_enabled AND next_run_at IS NOT NULL) FROM subscriptions')
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1', stale) == 'skipped'
