"""Durable speech cache and exact message replacement in disposable PostgreSQL."""
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.services import illustrations,message_languages as languages,speech
from app.worker.delivery import process_delivery
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_message_languages_postgres import card,sender
from tests.test_postgres_integration import db as db,load_fixture,create_destination

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


def generator():
 return AsyncMock(return_value=(b'ID3'+b'a'*1000,10,'edge','fixture-voice'))


async def test_shared_generation_fanout_and_language_change_never_plays_old_audio(db,monkeypatch):
 c,settings,en,chat,row=await setup(db)
 ru,_,_=await load_fixture(c,db[2],'rus')
 await c.execute("UPDATE verses SET text='Русский стих.' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",ru['id'])
 image=await illustrations.store(c,row,en,jpeg(),'fixture')
 _,first=await card(c,en,chat,row,image=image)
 chat2=await create_destination(c,202)
 _,second=await card(c,en,chat2,row,image=image)
 assert first['audio_id']==second['audio_id'] and first['audio_id']
 generate=generator()
 assert await speech.process(c,first['audio_id'],generator=generate)=='ready'
 assert await speech.process(c,second['audio_id'],generator=generate)=='not_due'
 assert generate.await_count==1
 assert await speech.dispatch(c)==2
 # Change language while the old ready-audio edit is already queued.
 change=await languages.select(c,101,501,first['id'],'s',ru['id'])
 bot,transport=sender(c,settings,monkeypatch)
 assert await process_delivery(c,change,transport)=='sent'
 assert not any(m.id=='narration' for m in (bot.edit_message_text.await_args.kwargs['rich_message'].media or []))
 old_edits=await c.fetch("SELECT id FROM delivery_log WHERE payload_key LIKE 'audio:%' ORDER BY id")
 for edit in old_edits:
  assert await process_delivery(c,edit['id'],transport)=='sent'
 first_call=bot.edit_message_text.await_args_list[1].kwargs
 assert 'Русский стих.' in first_call['rich_message'].html and 'narration' not in first_call['rich_message'].html
 second_call=bot.edit_message_text.await_args_list[2].kwargs
 assert second_call['chat_id']==202 and 'tg://audio?id=narration' in second_call['rich_message'].html
 current=await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1',first['id'])
 assert current['audio_id']!=first['audio_id'] and current['audio_sent_id'] is None
 assert await speech.process(c,current['audio_id'],generator=generate)=='ready'
 assert await speech.dispatch(c)==1
 edit=await c.fetchval("SELECT id FROM delivery_log WHERE payload_key=$1",f"audio:{first['id']}:{current['audio_id']}")
 assert await process_delivery(c,edit,transport)=='sent'
 final=bot.edit_message_text.await_args.kwargs
 assert len(final['rich_message'].media)==2 and 'Русский стих.' in final['rich_message'].html
 assert final['reply_markup'].inline_keyboard and final['message_id']==501
 assert await c.fetchval('SELECT audio_sent_id=audio_id FROM reading_cards WHERE id=$1',first['id'])
 assert await c.fetchval('SELECT count(*) FROM audio_generation_attempts')==0
 assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0
 bot.send_rich_message.assert_not_awaited()


async def test_recovery_distinguishes_paid_ambiguity_from_free_retry(db):
 c,_,en,chat,row=await setup(db)
 _,first=await card(c,en,chat,row)
 await c.execute("UPDATE reading_audio SET state='running' WHERE id=$1",first['audio_id'])
 await speech.recover(c)
 assert await c.fetchval('SELECT state FROM reading_audio WHERE id=$1',first['audio_id'])=='retry'
 await speech.process(c,first['audio_id'],generator=generator())
 paid=replace(speech.SpeechSettings(),provider='openai',paid_enabled=True,key='fixture',daily_characters=10000,monthly_characters=10000)
 identifier=await speech.attach(c,first['id'],paid)
 async def timeout(*args):
  raise TimeoutError()
 assert await speech.process(c,identifier,paid,generator=timeout)=='uncertain'
 assert await speech.process(c,identifier,paid,generator=generator())=='not_due'
 assert await c.fetchval('SELECT state FROM audio_generation_attempts')=='uncertain'
 assert await c.fetchval('SELECT characters FROM audio_generation_attempts')==len(await c.fetchval('SELECT source_text FROM reading_audio WHERE id=$1',identifier))


async def test_paid_budget_reservation_is_serialized_and_disabled_defaults(db):
 c,settings,en,chat,row=await setup(db)
 _,first=await card(c,en,chat,row)
 generate=generator()
 paid=replace(speech.SpeechSettings(),provider='openai',key='fixture')
 identifier=await speech.attach(c,first['id'],paid)
 assert await speech.process(c,identifier,paid,generator=generate)=='paid_disabled_or_limit'
 generate.assert_not_awaited()
 assert await c.fetchval('SELECT count(*) FROM audio_generation_attempts')==0
 paid=replace(paid,paid_enabled=True,daily_characters=len(row['text']),monthly_characters=len(row['text']))
 audio=await c.fetchrow('SELECT * FROM reading_audio WHERE id=$1',identifier)
 other=await asyncpg.connect(settings.database_url)
 try:
  import asyncio
  reservations=await asyncio.gather(speech.reserve_paid(c,audio,paid),speech.reserve_paid(other,audio,paid))
  assert sum(x is not None for x in reservations)==1
 finally:
  await other.close()


async def test_restart_late_image_and_not_modified_ack_keep_audio(db,monkeypatch):
 c,settings,en,chat,row=await setup(db)
 _,first=await card(c,en,chat,row)
 await speech.process(c,first['audio_id'],generator=generator())
 await speech.dispatch(c)
 edit=await c.fetchval("SELECT id FROM delivery_log WHERE payload_key LIKE 'audio:%'")
 bot,transport=sender(c,settings,monkeypatch)
 bot.edit_message_text.side_effect=TelegramBadRequest(method=EditMessageText(chat_id=101,message_id=501,text='fixture'),message='Bad Request: message is not modified')
 assert await process_delivery(c,edit,transport)=='sent'
 assert await c.fetchval('SELECT audio_sent_id=audio_id FROM reading_cards WHERE id=$1',first['id'])
 assert await speech.dispatch(c)==0
 # A late illustration update still reads the audio selection at actual send time.
 await c.execute("UPDATE reading_audio SET telegram_file_id='cached-audio',telegram_bot_id=$2 WHERE id=$1",first['audio_id'],bot.id)
 image=await illustrations.store(c,row,en,jpeg(),'late')
 await c.execute('UPDATE reading_cards SET image_id=$2 WHERE id=$1',first['id'],image)
 bot.edit_message_text.side_effect=None
 bot.edit_message_text.return_value=type('Response',(),{'message_id':501,'rich_message':type('Rich',(),{'blocks':[]})()})()
 change=await languages.select(c,101,501,first['id'],'p',0)
 assert await process_delivery(c,change,transport)=='sent'
 media=bot.edit_message_text.await_args.kwargs['rich_message'].media
 assert len(media)==2
 assert media[0].media.media is not None and media[1].media.media=='cached-audio'


async def test_page_and_language_roundtrip_reuses_cache_without_progress_changes(db):
 c,_,en,chat,row=await setup(db)
 ru,_,_=await load_fixture(c,db[2],'rus')
 await c.execute("UPDATE verses SET text=repeat('Очень длинный стих. ',2000) WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",ru['id'])
 _,first=await card(c,en,chat,row)
 await languages.select(c,101,501,first['id'],'s',ru['id'])
 before=await c.fetchval('SELECT audio_id FROM reading_cards WHERE id=$1',first['id'])
 await languages.select(c,101,501,first['id'],'t',1)
 page=await c.fetchval('SELECT audio_id FROM reading_cards WHERE id=$1',first['id'])
 assert before!=page
 await languages.select(c,101,501,first['id'],'s',en['id'])
 assert await c.fetchval('SELECT audio_id FROM reading_cards WHERE id=$1',first['id'])==first['audio_id']
 assert await c.fetchval('SELECT count(*) FROM chat_reading_progress')==0
 assert await c.fetchval('SELECT default_translation_id FROM telegram_chats WHERE telegram_chat_id=101')==en['id']


async def test_unbound_audio_is_not_generated_and_invalid_output_never_attaches(db):
 c,_,en,chat,row=await setup(db)
 _,first=await card(c,en,chat,row)
 await c.execute('UPDATE reading_cards SET telegram_message_id=NULL WHERE id=$1',first['id'])
 generate=generator()
 assert await speech.process(c,first['audio_id'],generator=generate)=='unbound'
 generate.assert_not_awaited()
 await languages.bind(c,first['id'],101,501)
 invalid=AsyncMock(return_value=(b'empty',0,'edge','voice'))
 assert await speech.process(c,first['audio_id'],generator=invalid)=='retry'
 assert await speech.dispatch(c)==0
