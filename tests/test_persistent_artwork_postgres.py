"""Late shared images update all original cards, independently of days/settings."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import artwork,bible,illustrations
from app.services.destinations import configure_chat
from app.services.errors import SendError
from app.worker.delivery import decoded,insert_payload,process_delivery,recover_ambiguous
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import create_destination,db as db

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


async def request(c,row,edition,chat,key,target):
    assert await artwork.request_image(c,row,edition,chat,f'Original {key}',key)
    r=await c.fetchrow('SELECT * FROM illustration_requests WHERE telegram_chat_id=$1 AND request_key=$2',chat['telegram_chat_id'],key)
    await illustrations.bind_message(c,chat['telegram_chat_id'],r['id'],target)
    return r


@pytest.mark.parametrize('scope',['verse','chapter'])
async def test_week_old_cards_across_chats_share_one_generation_and_all_update(db,scope):
    c,_,edition,chat,row=await setup(db)
    if scope=='chapter':row=illustrations.chapter_source(await bible.chapter_rows(c,edition['id'],'GEN',1))
    second=await create_destination(c,202)
    for index,dest in enumerate([chat,second,chat]):
        await request(c,row,edition,dest,str(index),400+index)
    await c.execute("UPDATE illustration_requests SET created_at=now()-interval '7 days',expires_at=now()-interval '6 days'")
    await c.execute('UPDATE telegram_chats SET revision=revision+3 WHERE telegram_chat_id=202')
    jobs=await artwork.due_jobs(c)
    assert len(jobs)==1
    generate=AsyncMock(return_value=(jpeg(),{},'fixture'))
    assert await artwork.process_job(c,jobs[0]['id'],artwork.ArtSettings(key='fixture'),generator=generate)=='ready'
    assert await artwork.dispatch_requests(c,maximum=1)==1
    assert await artwork.dispatch_requests(c,maximum=1)==1
    assert await artwork.dispatch_requests(c,maximum=1)==1
    assert await artwork.dispatch_requests(c)==0
    for delivery in await c.fetch("SELECT * FROM delivery_log WHERE mode='illustration_edit' ORDER BY id"):
        part=decoded(delivery['chunks'])[0]
        sender=SimpleNamespace(send=AsyncMock(return_value=part['message_id']))
        assert await process_delivery(c,delivery['id'],sender)=='sent'
        assert sender.send.await_args.args[1]['kind']=='rich_edit'
    assert await c.fetchval("SELECT count(*) FROM illustration_requests WHERE state='delivered'")==3
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==1
    assert await c.fetchval('SELECT count(*) FROM chat_reading_progress')==0
    generate.assert_awaited_once()


async def test_disabled_provider_still_records_card_for_later_manual_or_api_image(db,monkeypatch):
    monkeypatch.setenv('ILLUSTRATION_PROVIDER','manual');monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    c,_,edition,chat,row=await setup(db)
    text=await illustrations.decorate(c,'Original text',row,edition,chat=chat,request_key='no-provider')
    assert text.request_id and text.image_id is None
    await illustrations.bind_message(c,101,text.request_id,456)
    assert await c.fetchval('SELECT expires_at FROM illustration_requests') is None
    await illustrations.store(c,row,edition,jpeg(),'manual import')
    assert await artwork.dispatch_requests(c)==1
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


async def test_settings_change_after_queue_preserves_edit_and_cancels_only_new_sends(db):
    c,_,edition,chat,row=await setup(db)
    await request(c,row,edition,chat,'settings',456)
    await illustrations.store(c,row,edition,jpeg(),'source')
    assert await artwork.dispatch_requests(c)==1
    edit=await c.fetchval("SELECT id FROM delivery_log WHERE mode='illustration_edit'")
    ordinary=await insert_payload(c,chat,edition,'New reading','ordinary','manual',{'kind':'reading'})
    await configure_chat(c,101,actor_id=101,ui_language='ru')
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1',ordinary)=='cancelled'
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1',edit)=='pending'
    assert await process_delivery(c,edit,SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
    assert await c.fetchval('SELECT state FROM illustration_requests')=='delivered'


async def test_source_change_after_queue_is_checked_again_before_network_edit(db):
    c,_,edition,chat,row=await setup(db)
    await request(c,row,edition,chat,'source',456)
    await illustrations.store(c,row,edition,jpeg(),'source')
    await artwork.dispatch_requests(c)
    edit=await c.fetchval('SELECT id FROM delivery_log')
    await c.execute("UPDATE verses SET text=text||' changed' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",edition['id'])
    sender=SimpleNamespace(send=AsyncMock())
    assert await process_delivery(c,edit,sender)=='source_changed'
    sender.send.assert_not_awaited()
    assert await c.fetchval('SELECT state FROM illustration_requests')=='cancelled'


async def test_edit_retries_beyond_ten_network_failures_and_recovers_same_target(db):
    c,_,edition,chat,row=await setup(db)
    await request(c,row,edition,chat,'network',456)
    await illustrations.store(c,row,edition,jpeg(),'source')
    await artwork.dispatch_requests(c)
    edit=await c.fetchval('SELECT id FROM delivery_log')
    await c.execute('UPDATE delivery_log SET consecutive_failures=12')
    sender=SimpleNamespace(send=AsyncMock(side_effect=SendError('retry',2)))
    assert await process_delivery(c,edit,sender)=='retry'
    failed=await c.fetchrow('SELECT * FROM delivery_log')
    assert failed['status']=='retry' and failed['retry_at']>failed['updated_at']
    await c.execute("UPDATE delivery_log SET status='sending',sending_chunk=0")
    await recover_ambiguous(c)
    assert await process_delivery(c,edit,SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
    assert await c.fetchval('SELECT state FROM illustration_requests')=='delivered'
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==1
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


@pytest.mark.parametrize('kind',['rejected','forbidden'])
async def test_unavailable_user_does_not_block_other_users_and_blocked_chat_can_resume(db,kind):
    c,_,edition,chat,row=await setup(db)
    other=await create_destination(c,202)
    await request(c,row,edition,chat,'first',456)
    await request(c,row,edition,other,'other',567)
    await illustrations.store(c,row,edition,jpeg(),'source')
    assert await artwork.dispatch_requests(c)==2
    first=await c.fetchval('SELECT delivery_id FROM illustration_requests WHERE telegram_chat_id=101')
    second=await c.fetchval('SELECT delivery_id FROM illustration_requests WHERE telegram_chat_id=202')
    assert await process_delivery(c,first,SimpleNamespace(send=AsyncMock(side_effect=SendError(kind))))==kind
    assert await process_delivery(c,second,SimpleNamespace(send=AsyncMock(return_value=567)))=='sent'
    assert await c.fetchval('SELECT state FROM illustration_requests WHERE telegram_chat_id=202')=='delivered'
    if kind=='rejected':
        assert await c.fetchval('SELECT state FROM illustration_requests WHERE telegram_chat_id=101')=='unavailable'
    else:
        assert await c.fetchval('SELECT is_active FROM telegram_chats WHERE telegram_chat_id=101') is False
        await c.execute('UPDATE telegram_chats SET is_active=true WHERE telegram_chat_id=101')
        await c.execute('UPDATE delivery_log SET retry_at=now() WHERE id=$1',first)
        assert await process_delivery(c,first,SimpleNamespace(send=AsyncMock(return_value=456)))=='sent'
        assert await c.fetchval('SELECT state FROM illustration_requests WHERE telegram_chat_id=101')=='delivered'
    assert await artwork.dispatch_requests(c)==0


async def test_blocked_waiter_is_preserved_without_starving_other_active_chat(db):
    c,_,edition,chat,row=await setup(db)
    other=await create_destination(c,202)
    await request(c,row,edition,chat,'blocked',456)
    await request(c,row,edition,other,'active',567)
    await c.execute('UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=101')
    await illustrations.store(c,row,edition,jpeg(),'source')
    assert await artwork.dispatch_requests(c,maximum=1)==1
    assert await c.fetchval('SELECT state FROM illustration_requests WHERE telegram_chat_id=101')=='waiting'
    await c.execute('UPDATE telegram_chats SET is_active=true WHERE telegram_chat_id=101')
    assert await artwork.dispatch_requests(c,maximum=1)==1


async def test_later_replacement_image_updates_cards_waiting_on_failed_version(db):
    c,_,edition,chat,row=await setup(db)
    r=await request(c,row,edition,chat,'replacement',456)
    await c.execute("UPDATE verse_illustrations SET status='failed' WHERE id=$1",r['image_id'])
    replacement=await illustrations.store(c,row,edition,jpeg(),'new successful version')
    assert replacement!=r['image_id']
    assert await artwork.dispatch_requests(c)==1
    assert await c.fetchval('SELECT image_id FROM illustration_requests')==replacement
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


async def test_upgrade_revives_bound_expired_cards_and_interrupted_edit_only(db):
    c,_,edition,chat,row=await setup(db)
    old=await request(c,row,edition,chat,'expired',456)
    await request(c,row,edition,chat,'queued',567)
    await illustrations.store(c,row,edition,jpeg(),'ready')
    await artwork.dispatch_requests(c)
    await c.execute("UPDATE illustration_requests SET state='expired',delivery_id=NULL,expires_at=now()-interval '1 day' WHERE id=$1",old['id'])
    await c.execute("UPDATE delivery_log SET status='cancelled',error_code='configuration_changed' WHERE id=(SELECT delivery_id FROM illustration_requests WHERE request_key='queued')")
    # Simulate a genuinely expired card: its old delivery never existed.
    await c.execute("DELETE FROM delivery_log WHERE payload_key=$1",f"illustration-request:{old['id']}")
    migration=Path(__file__).resolve().parents[1]/'sql/migrations/010_persistent_artwork_waiters.sql'
    attempts=await c.fetchval('SELECT count(*) FROM image_generation_attempts')
    await c.execute(migration.read_text(encoding='utf-8'))
    assert await c.fetchval('SELECT state FROM illustration_requests WHERE id=$1',old['id'])=='waiting'
    assert await c.fetchval("SELECT status FROM delivery_log WHERE id=(SELECT delivery_id FROM illustration_requests WHERE request_key='queued')")=='retry'
    assert await artwork.dispatch_requests(c)==1
    for d in await c.fetch('SELECT * FROM delivery_log'):
        target=decoded(d['chunks'])[0]['message_id']
        assert await process_delivery(c,d['id'],SimpleNamespace(send=AsyncMock(return_value=target)))=='sent'
    assert await c.fetchval("SELECT count(*) FROM illustration_requests WHERE state='delivered'")==2
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==attempts
