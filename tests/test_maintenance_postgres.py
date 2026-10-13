import os

import pytest

from app.maintenance import RULES, prune
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')
RULE = {r.name: r for r in RULES}


async def add_delivery(c, key, status, age_days):
    return await c.fetchval("""INSERT INTO delivery_log(telegram_chat_id,mode,scheduled_for,payload_key,status,updated_at,chunks,progress)
        VALUES(101,'manual',now(),$1,$2,now()-make_interval(days=>$3),'[]'::jsonb,'{}'::jsonb) RETURNING id""", key, status, age_days)


@pytest.mark.asyncio
async def test_prune_keeps_recent_outstanding_and_referenced_rows(db):
    c, _, tmp = db
    await create_destination(c)
    translation, _, _ = await load_fixture(c, tmp)
    old_sent = await add_delivery(c, 'a', 'sent', 400)
    await add_delivery(c, 'b', 'sent', 10)
    await add_delivery(c, 'c', 'retry', 900)
    await add_delivery(c, 'd', 'failed', 900)
    referenced = await add_delivery(c, 'e', 'sent', 900)
    await c.execute("INSERT INTO operator_events(action,created_at) VALUES('old',now()-interval '400 days'),('new',now())")
    await c.execute("INSERT INTO usage_events(day,platform,event,count) VALUES(current_date-500,'telegram','cmd:x',1),(current_date,'telegram','cmd:x',1)")
    sub = await c.fetchval("""INSERT INTO subscriptions(telegram_chat_id,translation_id,mode,send_time,timezone,next_run_at)
        VALUES(101,$1,'sequential','09:00','UTC',now()) RETURNING id""", translation['id'])
    await c.execute("""INSERT INTO prayer_occurrences(subscription_id,subscription_revision,chat_revision,local_date,scheduled_for,delivery_id)
        VALUES($1,1,1,current_date,now(),$2)""", sub, referenced)
    assert await prune(c, RULE['delivery_log'], dry_run=True) == 1
    assert await prune(c, RULE['delivery_log']) == 1
    remaining = {r['payload_key'] for r in await c.fetch('SELECT payload_key FROM delivery_log')}
    assert remaining == {'b', 'c', 'd', 'e'}
    assert old_sent not in {r['id'] for r in await c.fetch('SELECT id FROM delivery_log')}
    assert await prune(c, RULE['operator_events']) == 1
    assert await prune(c, RULE['usage_events']) == 1
    assert await c.fetchval('SELECT count(*) FROM operator_events') >= 1


@pytest.mark.asyncio
async def test_prune_inbox_done_only(db):
    c = db[0]
    await c.execute("""INSERT INTO max_inbox(event_key,payload,status,updated_at) VALUES
        ('k1','{}','done',now()-interval '30 days'),('k2','{}','done',now()),('k3','{}','pending',now()-interval '30 days')""")
    assert await prune(c, RULE['max_inbox']) == 1
    assert await c.fetchval('SELECT count(*) FROM max_inbox') == 2
