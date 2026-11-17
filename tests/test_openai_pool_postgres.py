import asyncio
import os
from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import artwork, bible, image_router as router
from app.services import image_providers as api
from tests.test_devotional_artwork import jpeg
from tests.test_image_router_postgres import job
from tests.test_postgres_integration import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


def pool(**kwargs):
    return artwork.ArtSettings(provider='auto',key='first-fixture',openai_keys=('first-fixture','second-fixture'),
                               provider_order=('openai',), **kwargs)


async def second_job(c):
    image = await c.fetchrow('SELECT * FROM verse_illustrations ORDER BY id LIMIT 1')
    edition = await bible.find_translation(c,image['translation_id'])
    row = await c.fetchrow("SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",edition['id'])
    await artwork.enqueue(c,row,edition)
    return await c.fetchval('SELECT max(id) FROM image_generation_jobs')


async def test_admin_openai_readiness_uses_current_keys_and_separates_queue_history(db, monkeypatch):
    from tests.test_admin_api_postgres import H, make
    from app.services.subscriptions import create_or_update_subscription
    from app.worker.delivery import insert_payload

    c, _, archived = await job(db)
    current = await second_job(c)
    await c.execute("""UPDATE image_generation_jobs SET state='failed',error_code='quality_unavailable',
        updated_at=now()-interval '7 days' WHERE id=$1""", archived)
    image = await c.fetchrow('SELECT i.* FROM verse_illustrations i JOIN image_generation_jobs j ON j.image_id=i.id WHERE j.id=$1', archived)
    chat = await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    await c.execute("""INSERT INTO illustration_requests(telegram_chat_id,image_id,request_key,caption,chat_revision,telegram_message_id)
        VALUES(101,$1,'waiting-card','Fixture',$2,456)""", image['id'], chat['revision'])
    await c.execute("UPDATE image_generation_jobs SET admin_requested=true WHERE id=$1", current)
    await c.execute("INSERT INTO service_heartbeats(service) VALUES('illustrator')")
    key = 'active-fixture'
    active = router.fingerprint(api.Provider('openai', key))
    expired = router.fingerprint(api.Provider('openai', 'old-fixture'))
    await c.execute("""INSERT INTO image_provider_health(provider,key_fingerprint,blocked_until,error_code)
        VALUES('openai',$1,now()+interval '1 day','auth')""", expired)
    success = await c.fetchval("""INSERT INTO image_generation_attempts
        (job_id,reserved_usd,provider,key_fingerprint,state,created_at)
        VALUES($1,0.10,'openai',$2,'succeeded',now()-interval '3 minutes') RETURNING created_at""", archived, active)
    await c.execute("""INSERT INTO image_generation_attempts
        (job_id,reserved_usd,provider,key_fingerprint,state,error_code)
        VALUES($1,0.10,'openai',$2,'succeeded','quality_unavailable')""", current, active)
    monkeypatch.setenv('ILLUSTRATION_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setenv('OPENAI_API_KEYS', key)
    monkeypatch.setenv('IMAGE_MONTHLY_BUDGET_USD', '10')
    monkeypatch.setenv('IMAGE_DAILY_MAX_REQUESTS', '6')
    async with make(monkeypatch, c) as client:
        response = await client.get('/api/admin/image-providers', headers=H)
        assert response.status_code == 200
        data = response.json()
        assert data['openai_readiness'] == {
            'status': 'available', 'configured_keys': 1, 'worker_ready': True,
            'blocked_until': None, 'error_code': None, 'last_success_at': success.isoformat(),
        }
        assert data['builtin'][0]['configured'] and data['builtin'][0]['blocked_until'] is None
        assert data['queue'] == {'current': {'queued': 1}, 'history': {'failed': 1}}
        assert key not in response.text and active not in response.text
        await c.execute('UPDATE image_generation_jobs SET updated_at=now() WHERE id=$1', archived)
        assert (await artwork.queue_counts(c))['current']['failed'] == 1
        await c.execute("UPDATE image_generation_jobs SET updated_at=now()-interval '7 days' WHERE id=$1", archived)
        edition = await bible.find_translation(c, image['translation_id'])
        subscription = await create_or_update_subscription(c, chat_id=101, created_by=101,
            translation_id=edition['id'], mode='verse_of_day', send_time=time(14), timezone_name=chat['timezone'])
        delivery = await insert_payload(c, chat, edition, 'Fixture', 'scheduled-test', 'verse_of_day', {},
            subscription=subscription, frozen_chunks=[{'kind': 'text', 'text': 'Fixture'}])
        await c.execute('INSERT INTO scheduled_readings(delivery_id,image_id,deadline_at) VALUES($1,$2,$3)',
            delivery, image['id'], subscription['next_run_at']+timedelta(minutes=30))
        assert (await artwork.queue_counts(c)) == {'current': {'failed': 1, 'queued': 1}, 'history': {}}
        await c.execute("""INSERT INTO image_provider_health(provider,key_fingerprint,blocked_until,error_code)
            VALUES('openai',$1,now()+interval '1 hour','quota')""", active)
        blocked = (await client.get('/api/admin/image-providers', headers=H)).json()['openai_readiness']
        assert blocked['status'] == 'blocked' and blocked['error_code'] == 'quota' and blocked['blocked_until']


async def test_bad_key_does_not_block_good_key_and_restart_preserves_breakers(db):
    c, _, identifier = await job(db)
    settings = pool()
    await router.configure(c,settings)
    generator = AsyncMock(side_effect=[api.GenerationError('quota'), (jpeg(),{},'good-result')])
    assert await router.process_job(c,identifier,settings,generator=generator) == 'quota'
    assert await router.process_job(c,identifier,settings,generator=generator) == 'ready'
    assert [call.args[0].key for call in generator.await_args_list] == ['first-fixture','second-fixture']
    await router.configure(c,replace(settings,key='',openai_keys=('second-fixture',)))
    await router.configure(c,settings)
    first = router.fingerprint(api.Provider('openai','first-fixture'))
    assert await c.fetchval("SELECT blocked_until>now() FROM image_provider_health WHERE provider='openai' AND key_fingerprint=$1",first)
    assert await c.fetchval('SELECT sum(reserved_usd) FROM image_generation_attempts') == Decimal('0.20')
    next_id = await second_job(c)
    assert await router.process_job(c,next_id,settings,generator=AsyncMock(return_value=(jpeg(),{},'next'))) == 'ready'
    assert await c.fetchval('SELECT key_fingerprint FROM image_generation_attempts WHERE job_id=$1',next_id) != first


@pytest.mark.parametrize('scope,monthly,expected',[('provider','10',1),('key','10',2),('key','0.10',1)])
async def test_concurrent_jobs_obey_scope_and_global_budget(db,scope,monthly,expected):
    c, database, first = await job(db)
    second = await second_job(c)
    settings = pool(daily_requests=1,openai_daily_scope=scope,monthly_usd=Decimal(monthly))
    other = await asyncpg.connect(database.database_url)
    try:
        results = await asyncio.gather(router.reserve(c,first,settings),router.reserve(other,second,settings))
        assert sum(result[0] is not None for result in results) == expected
        if expected == 2:
            assert len({router.fingerprint(result[1]) for result in results}) == 2
        assert await c.fetchval('SELECT sum(reserved_usd) FROM image_generation_attempts') == Decimal('0.10')*expected
    finally:
        await other.close()


async def test_more_than_five_keys_reaches_valid_key_with_bounded_attempts(db):
    from tests.test_persistent_artwork_postgres import request
    c, _, identifier = await job(db)
    image = await c.fetchrow('SELECT * FROM verse_illustrations')
    edition = await bible.find_translation(c,image['translation_id'])
    row = await c.fetchrow("SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",edition['id'])
    chat = await c.fetchrow('SELECT * FROM telegram_chats ORDER BY telegram_chat_id LIMIT 1')
    await request(c,row,edition,chat,'many-keys',401)
    settings = replace(pool(),key='',openai_keys=tuple('fixture-'+str(i) for i in range(7)))
    generator = AsyncMock(side_effect=[api.GenerationError('auth') for _ in range(6)]+[(jpeg(),{},'last-good-key')])
    for _ in range(6):
        assert await router.process_job(c,identifier,settings,generator=generator) == 'auth'
        assert await artwork.due_jobs(c,max_attempts=router.attempt_limit(settings))
    assert await router.process_job(c,identifier,settings,generator=generator) == 'ready'
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts') == 7
    assert len({call.args[0].key for call in generator.await_args_list}) == 7


@pytest.mark.parametrize('error,expected',[(api.GenerationError('transport_uncertain',uncertain=True), 'ready'),
                                        (api.GenerationError('moderation'),'budget_or_not_due')])
async def test_uncertainty_keeps_reserve_and_moderation_remains_terminal(db,error,expected):
    c, _, identifier = await job(db)
    settings = pool()
    generator = AsyncMock(side_effect=[error,(jpeg(),{},'second')])
    assert await router.process_job(c,identifier,settings,generator=generator) == error.code
    assert await router.process_job(c,identifier,settings,generator=generator) == expected
    assert generator.await_count == (2 if expected == 'ready' else 1)
    assert await c.fetchval('SELECT state FROM image_generation_attempts ORDER BY id LIMIT 1') == ('uncertain' if error.uncertain else 'failed')


async def test_diagnostics_show_each_key_without_secrets(db,monkeypatch):
    from app.illustrations_admin import run
    c, database, identifier = await job(db)
    monkeypatch.setenv('DATABASE_URL',database.database_url)
    monkeypatch.setenv('ILLUSTRATION_PROVIDER','auto')
    monkeypatch.setenv('OPENAI_API_KEY','first-fixture')
    monkeypatch.setenv('OPENAI_API_KEYS','second-fixture')
    settings = artwork.ArtSettings.from_env()
    await router.configure(c,settings)
    await router.process_job(c,identifier,settings,generator=AsyncMock(return_value=(jpeg(),{},'first')))
    status = await run(SimpleNamespace(command='providers'))
    openai = next(p for p in status['providers'] if p['provider'] == 'openai')
    assert openai['configured_keys'] == 2 and openai['daily_requests_used'] == 1
    assert [k['daily_requests_used'] for k in openai['credentials']] == [1,0]
    assert 'first-fixture' not in str(status) and 'second-fixture' not in str(status)


async def test_legacy_unassigned_attempts_cannot_reset_daily_allowance(db):
    c, _, first = await job(db)
    second = await second_job(c)
    for _ in range(3):
        await c.execute("INSERT INTO image_generation_attempts(job_id,reserved_usd,provider,state) VALUES($1,0.10,'openai','failed')",first)
    settings = pool(daily_requests=3,openai_daily_scope='key')
    attempt, _, reason = await router.reserve(c,second,settings)
    assert attempt is None and reason == 'budget_or_not_due'
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts') == 3
