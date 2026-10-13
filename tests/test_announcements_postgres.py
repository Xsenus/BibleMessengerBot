"""Объявления в БД: запуск по расписанию, повторы, пропуск опоздавших, паузы и восстановление."""
import os
from datetime import timedelta

import pytest

from app.services import announcements as a
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


async def setup(c):
    chat = await create_destination(c, 101)
    await create_destination(c, -100700, 'ru')
    await c.execute("UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101")
    return dict(chat)


async def runs(c, ann):
    return await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='announcement' AND payload_key LIKE 'announcement:%'"), \
        await c.fetchval('SELECT status FROM announcements WHERE id=$1', ann)


@pytest.mark.asyncio
async def test_now_announcement_goes_to_everyone_once(db):
    c = db[0]
    chat = await setup(c)
    text, _ = await a.command(c, 101, chat, 'now | Анонс | Встреча в субботу')
    assert 'создано' in text or 'created' in text
    ann = await c.fetchrow('SELECT * FROM announcements')
    assert await a.run_due(c) == 1
    assert await a.run_due(c) == 0                                           # повторного запуска нет
    count, status = await runs(c, ann['id'])
    assert count == 2 and status == 'finished'
    chunk = await c.fetchval("SELECT chunks FROM delivery_log WHERE mode='announcement' AND telegram_chat_id=101")
    assert 'Анонс' in str(chunk) and 'Встреча' in str(chunk)


@pytest.mark.asyncio
async def test_weekly_repeats_and_respects_max_runs(db):
    c = db[0]
    chat = await setup(c)
    await a.command(c, 101, chat, 'weekly sat 10:00 Europe/Moscow x2 | Субботнее | Текст')
    ann = await c.fetchrow('SELECT * FROM announcements')
    assert ann['days_of_week'] == [6] and ann['max_runs'] == 2
    for _ in range(2):
        row = await c.fetchrow('SELECT * FROM announcements')
        assert row['status'] == 'active'
        await a.run_one(c, row['id'], row['next_run_at'] + timedelta(seconds=30))   # наступил момент запуска
    final = await c.fetchrow('SELECT * FROM announcements')
    assert final['runs_done'] == 2 and final['status'] == 'finished' and final['next_run_at'] is None
    assert await c.fetchval("SELECT count(*) FROM announcement_runs WHERE status='done'") == 2


@pytest.mark.asyncio
async def test_late_run_is_skipped_pause_and_recovery(db):
    c = db[0]
    chat = await setup(c)
    await a.command(c, 101, chat, 'daily 09:00 Europe/Moscow | Утро | Доброе утро')
    row = await c.fetchrow('SELECT * FROM announcements')
    far = row['next_run_at'] + timedelta(hours=5)                                    # служба простояла: запуск опоздал
    assert await a.run_one(c, row['id'], far) is None
    after = await c.fetchrow('SELECT * FROM announcements')
    assert after['runs_done'] == 0 and after['next_run_at'] > far
    assert await c.fetchval("SELECT status FROM announcement_runs") == 'skipped_late'
    assert await a.set_status(c, row['id'], 'paused')
    assert await a.run_one(c, row['id'], after['next_run_at'] + timedelta(seconds=5)) is None
    # прерванный запуск дорассылается без дублей
    run = await c.fetchval("INSERT INTO announcement_runs(announcement_id,scheduled_for,status,created_at) VALUES($1,now()-interval '1 hour','sending',now()-interval '1 hour') RETURNING id", row['id'])
    await a.run_due(c)
    await a.run_due(c)
    assert await c.fetchval('SELECT status FROM announcement_runs WHERE id=$1', run) == 'done'
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE payload_key=$1", f'announcement:{run}') == 2


@pytest.mark.asyncio
async def test_only_admin_in_private_chat_can_manage(db):
    c = db[0]
    chat = await setup(c)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,timezone) VALUES(777,'UTC')")
    with pytest.raises(UserError):
        await a.command(c, 777, chat, 'now | x | y')
    with pytest.raises(UserError):
        await a.command(c, 101, dict(chat, chat_type='supergroup'), 'list')
    text, markup = await a.command(c, 101, chat, 'list')
    assert 'nnounce' in text or 'Объявления' in text
