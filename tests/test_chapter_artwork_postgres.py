from tests.bot_patch import patch_bot
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import artwork,bible,illustrations
from app.bot import handlers
from app.bot.commands import parse_command
from app.services.subscriptions import create_or_update_subscription
from datetime import time
from app.worker.delivery import decoded,enqueue_next,process_delivery,prepare_subscription
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import provider,setup
from tests.test_postgres_integration import db as db

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


async def test_manual_chapter_freezes_full_source_reuses_one_job_and_edits_original(db,monkeypatch):
    provider(monkeypatch)
    c,settings,edition,chat,anchor=await setup(db)
    verse_image=await illustrations.store(c,anchor,edition,jpeg(),'old verse')
    delivery=await enqueue_next(c,chat,edition,'chapter-1')
    assert await enqueue_next(c,chat,edition,'chapter-1')==delivery
    initial=decoded(await c.fetchval('SELECT chunks FROM delivery_log WHERE id=$1',delivery))
    assert initial[0]['kind']=='rich' and initial[0]['image_id'] is None
    assert await c.fetchval("SELECT count(*) FROM image_generation_jobs")==1
    request=await c.fetchrow('SELECT * FROM illustration_requests')
    assert len(decoded(request['source_snapshot']))==2
    image=await c.fetchrow('SELECT * FROM verse_illustrations WHERE id=$1',request['image_id'])
    assert image['artwork_scope']=='chapter' and image['id']!=verse_image
    assert await artwork.dispatch_requests(c)==0
    assert await process_delivery(c,delivery,SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
    assert await c.fetchval('SELECT chapter FROM chat_reading_progress')==1
    job=await c.fetchval('SELECT id FROM image_generation_jobs')
    generator=AsyncMock(return_value=(jpeg(),{},'fixture-request'))
    assert await artwork.process_job(c,job,artwork.ArtSettings(key='fixture'),generator=generator)=='ready'
    assert generator.await_count==1
    assert await artwork.dispatch_requests(c)==1
    edit=await c.fetchrow("SELECT * FROM delivery_log WHERE mode='illustration_edit'")
    assert decoded(edit['chunks'])[0]['message_id']==456
    assert await process_delivery(c,edit['id'],SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
    assert await c.fetchval('SELECT chapter FROM chat_reading_progress')==1
    text=await bible.render_chapter(c,edition,'GEN',1)
    repeated=await illustrations.decorate_chapter(c,text,edition,'GEN',1,chat=chat,request_key='again')
    assert repeated.image_id==image['id'] and repeated.request_id is None
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==1
    assert await c.fetchval('SELECT image_data FROM verse_illustrations WHERE id=$1',verse_image)==jpeg()


@pytest.mark.parametrize('generate_first',[False,True])
async def test_chapter_change_rejects_generation_or_edit_without_new_spend(db,monkeypatch,generate_first):
    provider(monkeypatch)
    c,_,edition,chat,_=await setup(db)
    delivery=await enqueue_next(c,chat,edition,'source-change')
    await process_delivery(c,delivery,SimpleNamespace(send=AsyncMock(return_value=456)))
    image=await c.fetchrow("SELECT * FROM verse_illustrations WHERE artwork_scope='chapter'")
    if generate_first:
        source=await illustrations.source_row(c,image)
        await illustrations.store(c,source,edition,jpeg(),'chapter',image_id=image['id'],prompt_version=3)
    await c.execute("UPDATE verses SET text=text||' changed' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",edition['id'])
    generator=AsyncMock()
    if not generate_first:
        job=await c.fetchval('SELECT id FROM image_generation_jobs')
        assert await artwork.process_job(c,job,artwork.ArtSettings(key='fixture'),generator=generator)=='source_changed'
        generator.assert_not_awaited()
    else:
        assert await artwork.dispatch_requests(c)==0
        assert await c.fetchval('SELECT state FROM illustration_requests')=='cancelled'
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


async def test_failed_generation_due_to_daily_cap_preserves_bound_chapter(db,monkeypatch):
    provider(monkeypatch)
    c,_,edition,chat,_=await setup(db)
    delivery=await enqueue_next(c,chat,edition,'budget')
    await process_delivery(c,delivery,SimpleNamespace(send=AsyncMock(return_value=456)))
    job=await c.fetchval('SELECT id FROM image_generation_jobs')
    await c.execute("INSERT INTO image_generation_attempts(job_id,reserved_usd,state) VALUES($1,0.1,'failed')",job)
    generator=AsyncMock()
    assert await artwork.process_job(c,job,artwork.ArtSettings(key='fixture',daily_requests=1),generator=generator)=='budget_or_not_due'
    generator.assert_not_awaited()
    request=await c.fetchrow('SELECT * FROM illustration_requests')
    assert request['state']=='waiting' and request['telegram_message_id']==456
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==1


@pytest.mark.parametrize('command',['/read Genesis 1','/search Genesis 1','/read Genesis 1:1–2'])
async def test_only_whole_chapter_addresses_use_chapter_artwork(db,monkeypatch,command):
    provider(monkeypatch)
    c,settings,edition,chat,_=await setup(db)
    patch_bot(monkeypatch, 'destination',AsyncMock(return_value=chat))
    message=SimpleNamespace(from_user=SimpleNamespace(id=101),chat=SimpleNamespace(id=101,type='private'),message_id=100,message_thread_id=None)
    text,_=await handlers.run_command(c,None,settings,message,parse_command(command))
    assert '<b>[1]</b>' in text and '<b>[2]</b>' in text
    whole=':' not in command
    assert isinstance(text,illustrations.ChapterText)==whole
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE artwork_scope='chapter'")==int(whole)


async def test_sequential_subscription_queues_chapter_and_advances_only_after_ack(db,monkeypatch):
    provider(monkeypatch)
    c,_,edition,chat,_=await setup(db)
    sub=await create_or_update_subscription(c,chat_id=101,created_by=101,translation_id=edition['id'],mode='sequential',send_time=time(9),timezone_name='UTC')
    await c.execute("UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1",sub['id'])
    delivery=await prepare_subscription(c,sub['id'])
    assert await c.fetchval('SELECT current_chapter FROM subscriptions WHERE id=$1',sub['id']) is None
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE artwork_scope='chapter'")==1
    assert await process_delivery(c,delivery,SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
    assert await c.fetchval('SELECT current_chapter FROM subscriptions WHERE id=$1',sub['id'])==1
    assert await c.fetchval('SELECT telegram_message_id FROM illustration_requests')==456
