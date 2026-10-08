import os
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock

import pytest

from app.services import bible, illustrations, message_languages, prayers, speech
from app.services.errors import SendError
from app.services.subscriptions import create_or_update_subscription, set_enabled
from app.worker.delivery import decoded, prepare_subscription, process_delivery
from tests.test_devotional_artwork import jpeg
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


async def setup(c,path,mode='morning_verse',future=True):
    edition,_,_=await load_fixture(c,path)
    await load_fixture(c,path,'rus')
    await create_destination(c,language='ru')
    sub=await create_or_update_subscription(c,chat_id=101,created_by=101,translation_id=edition['id'],mode=mode,send_time=time(9,13),timezone_name='UTC')
    await c.execute("UPDATE subscriptions SET next_run_at=now()+$2*interval '1 minute' WHERE id=$1",sub['id'],10 if future else -1)
    delivery=await prepare_subscription(c,sub['id'],lead_seconds=86400)
    row=await c.fetchrow('SELECT * FROM delivery_log WHERE id=$1',delivery)
    return sub,delivery,decoded(row['chunks'])[0]['card_id']


async def make_due(c,identifier):
    await c.execute("UPDATE delivery_log SET scheduled_for=now()-interval '1 second' WHERE id=$1",identifier)
    await c.execute("UPDATE scheduled_readings SET deadline_at=now()+interval '10 minutes' WHERE delivery_id=$1",identifier)


async def finish(c,identifier, *, audio=True,image=True):
    prepared=await c.fetchrow('SELECT * FROM scheduled_readings WHERE delivery_id=$1',identifier)
    if image:
        record=await c.fetchrow('SELECT * FROM verse_illustrations WHERE id=$1',prepared['image_id'])
        edition=await bible.find_translation(c,record['translation_id'])
        await illustrations.store(c,await illustrations.source_row(c,record),edition,jpeg(),'fixture',image_id=record['id'])
    if audio:
        for item in await c.fetch('SELECT DISTINCT t.audio_id FROM reading_card_tracks t JOIN reading_cards c ON c.id=t.card_id JOIN delivery_log d ON c.source_key LIKE \'outbox:\'||d.payload_key||\':%\' WHERE d.id=$1',identifier):
            assert await speech.process(c,item['audio_id'],generator=AsyncMock(return_value=(b'ID3'+b'x'*700,3,'fixture','fixture'))) == 'ready'


async def test_future_bundle_prepares_all_languages_without_send_or_paid_calls(db):
    c,_,path=db
    sub,identifier,card=await setup(c,path)
    tracks=await c.fetch('SELECT * FROM reading_card_tracks WHERE card_id=$1',card)
    assert len(tracks)==2
    assert await c.fetchval('SELECT telegram_message_id IS NULL FROM reading_cards WHERE id=$1',card)
    assert await c.fetchval('SELECT count(*) FROM prayer_occurrences')==1
    sender=AsyncMock()
    assert await process_delivery(c,identifier,sender)=='not_due'
    sender.send.assert_not_awaited()
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0
    assert await c.fetchval('SELECT count(*) FROM audio_generation_attempts')==0
    assert await prepare_subscription(c,sub['id'],lead_seconds=86400) is None


async def test_missing_photo_or_any_language_blocks_initial_submission(db):
    c,_,path=db
    _,identifier,card=await setup(c,path,future=False)
    await make_due(c,identifier)
    sender=AsyncMock()
    assert await process_delivery(c,identifier,sender)=='preparing_media'
    await finish(c,identifier,audio=False)
    assert await process_delivery(c,identifier,sender)=='preparing_media'
    source=await c.fetchval('SELECT audio_id FROM reading_cards WHERE id=$1',card)
    await speech.process(c,source,generator=AsyncMock(return_value=(b'ID3'+b'x'*700,3,'fixture','fixture')))
    assert await process_delivery(c,identifier,sender)=='preparing_media'
    sender.send.assert_not_awaited()


async def test_ready_bundle_sends_once_and_language_switch_reuses_ready_audio(db):
    c,_,path=db
    _,identifier,card_id=await setup(c,path,future=False)
    await make_due(c,identifier)
    await finish(c,identifier)
    sender=AsyncMock();sender.send.return_value=777
    assert await process_delivery(c,identifier,sender)=='sent'
    assert await process_delivery(c,identifier,sender)=='not_due'
    sender.send.assert_awaited_once()
    card=await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1',card_id)
    outgoing,keyboard=await message_languages.outgoing(c,101,dict(kind='rich',card_id=card_id))
    assert outgoing['image_id'] and outgoing['audio_id'] and keyboard.inline_keyboard
    assert 'совместной молитвы' in outgoing['text']
    for edition in await message_languages.available(c,card):
        await message_languages.select(c,101,777,card_id,'s',edition['id'])
        fresh=await c.fetchrow('SELECT a.* FROM reading_cards c JOIN reading_audio a ON a.id=c.audio_id WHERE c.id=$1',card_id)
        assert fresh['state']=='ready' and fresh['attempts']==1
    assert await c.fetchval('SELECT count(*) FROM audio_generation_attempts')==0


async def test_audio_can_prepare_before_ack_but_not_after_cancel(db):
    c,_,path=db
    sub,identifier,card=await setup(c,path)
    ids=await c.fetch('SELECT audio_id FROM reading_card_tracks WHERE card_id=$1 ORDER BY audio_id',card)
    generator=AsyncMock(return_value=(b'ID3'+b'x'*700,3,'fixture','fixture'))
    assert await speech.process(c,ids[0]['audio_id'],generator=generator)=='ready'
    await set_enabled(c,101,False,sub['mode'])
    assert await speech.process(c,ids[1]['audio_id'],generator=generator)=='unbound'
    assert generator.await_count==1


async def test_expired_bundle_never_sends_partial_and_next_day_advances(db):
    c,_,path=db
    sub,identifier,_=await setup(c,path,future=False)
    await make_due(c,identifier)
    await c.execute("UPDATE scheduled_readings SET deadline_at=now()-interval '1 second' WHERE delivery_id=$1",identifier)
    sender=AsyncMock()
    assert await process_delivery(c,identifier,sender)=='expired'
    sender.send.assert_not_awaited()
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1',identifier)=='skipped'
    next_run=await c.fetchval('SELECT next_run_at FROM subscriptions WHERE id=$1',sub['id'])
    assert next_run>datetime.now(UTC) and next_run.time()==time(9)


async def test_prayer_sends_at_own_time_while_reading_media_pending(db,monkeypatch):
    c,_,path=db
    sub,identifier,_=await setup(c,path,future=False)
    await c.execute("UPDATE prayer_occurrences SET scheduled_for=now()+interval '1 minute',local_date=(now() AT TIME ZONE 'UTC')::date")
    monkeypatch.setattr(prayers,'brief',AsyncMock(return_value={'prayer_text':'Господи, даруй нам мир. Аминь.','generator':'template:no_current_news','news_snapshot':[]}))
    await prayers.prepare_due(c)
    prayer=await c.fetchval("SELECT id FROM delivery_log WHERE mode='prayer'")
    assert prayer
    sender=AsyncMock();sender.send.return_value=778
    assert await process_delivery(c,prayer,sender)=='not_due'
    await c.execute("UPDATE delivery_log SET scheduled_for=now()-interval '1 second' WHERE id=$1",prayer)
    before=await c.fetchval('SELECT next_run_at FROM subscriptions WHERE id=$1',sub['id'])
    assert await process_delivery(c,prayer,sender)=='sent'
    assert await c.fetchval('SELECT next_run_at FROM subscriptions WHERE id=$1',sub['id'])==before
    assert await c.fetchval('SELECT state FROM scheduled_readings WHERE delivery_id=$1',identifier)=='preparing'
    assert 'Давайте помолимся вместе' in sender.send.call_args.args[1]['text']


async def test_prayer_retry_after_and_uncertainty_do_not_duplicate(db,monkeypatch):
    c,_,path=db
    _,_,_=await setup(c,path,future=False)
    await c.execute("UPDATE prayer_occurrences SET scheduled_for=now(),local_date=(now() AT TIME ZONE 'UTC')::date")
    monkeypatch.setattr(prayers,'brief',AsyncMock(return_value={'prayer_text':'Господи, даруй нам мир. Аминь.','generator':'template:no_current_news','news_snapshot':[]}))
    await prayers.prepare_due(c)
    identifier=await c.fetchval("SELECT id FROM delivery_log WHERE mode='prayer'")
    sender=AsyncMock();sender.send.side_effect=SendError('retry',17)
    assert await process_delivery(c,identifier,sender)=='retry'
    assert await process_delivery(c,identifier,sender)=='not_due'
    row=await c.fetchrow('SELECT * FROM delivery_log WHERE id=$1',identifier)
    assert (row['retry_at']-datetime.now(UTC)).total_seconds()>15
    await c.execute("UPDATE delivery_log SET retry_at=now() WHERE id=$1",identifier)
    sender.send.side_effect=SendError('uncertain')
    assert await process_delivery(c,identifier,sender)=='uncertain'
    assert await process_delivery(c,identifier,sender)=='not_due'
    assert sender.send.await_count==2


async def test_prayer_does_not_arrive_after_expiry(db,monkeypatch):
    c,_,path=db
    await setup(c,path,future=False)
    await c.execute("UPDATE prayer_occurrences SET scheduled_for=now(),local_date=(now() AT TIME ZONE 'UTC')::date")
    monkeypatch.setattr(prayers,'brief',AsyncMock(return_value={'prayer_text':'Prayer','generator':'template','news_snapshot':[]}))
    await prayers.prepare_due(c)
    identifier=await c.fetchval("SELECT id FROM delivery_log WHERE mode='prayer'")
    await c.execute("UPDATE delivery_log SET scheduled_for=now()-interval '4 minutes' WHERE id=$1",identifier)
    sender=AsyncMock()
    assert await process_delivery(c,identifier,sender)=='expired'
    sender.send.assert_not_awaited()


async def test_prepared_image_with_changed_source_is_never_sent(db):
    c,_,path=db
    _,identifier,_=await setup(c,path,future=False)
    await make_due(c,identifier);await finish(c,identifier)
    await c.execute("UPDATE verses v SET text=v.text||'changed' FROM verse_illustrations i JOIN scheduled_readings r ON r.image_id=i.id WHERE r.delivery_id=$1 AND v.translation_id=i.translation_id AND v.book_code=i.book_code AND v.chapter=i.chapter AND v.verse=i.verse",identifier)
    sender=AsyncMock()
    assert await process_delivery(c,identifier,sender)=='source_changed'
    sender.send.assert_not_awaited()
