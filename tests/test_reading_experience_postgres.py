"""Persisted user choices and context navigation, with fake message transports."""
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import handlers
from app.bot.commands import parse_command
from app.services import devotionals, message_languages, speech
from app.services.accounts import upsert_chat
from tests.test_postgres_integration import db as db, create_destination, load_fixture
from tests.test_message_languages_postgres import card
from tests.test_scheduled_media_postgres import setup

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


async def test_voice_and_hidden_keyboard_persist_without_changing_progress(db,monkeypatch):
    c,settings,_=db
    chat=await create_destination(c,language='ru')
    async def destination(*args):
        return await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    monkeypatch.setattr(handlers,'destination',destination)
    message=SimpleNamespace(from_user=SimpleNamespace(id=101),chat=SimpleNamespace(id=101,type='private'))
    before=chat['revision']
    _,keyboard=await handlers.run_command(c,None,settings,message,parse_command('/hide_keyboard'))
    assert keyboard.remove_keyboard
    await handlers.run_command(c,None,settings,message,parse_command('/voice mary'))
    await upsert_chat(c,chat_id=101,chat_type='private',title='Fixture',username=None,registered_by=101,default_timezone='UTC',ui_language='ru')
    current=await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    assert current['keyboard_hidden'] and current['audio_voice']=='mary' and current['revision']==before
    await handlers.run_command(c,None,settings,message,parse_command('/menu'))
    assert not await c.fetchval('SELECT keyboard_hidden FROM telegram_chats WHERE telegram_chat_id=101')
    assert await c.fetchval('SELECT count(*) FROM chat_reading_progress')==0


async def test_future_tracks_follow_selected_voice_and_frozen_job_profile(db):
    c,_,path=db
    _,delivery,identifier=await setup(c,path)
    previous=await c.fetchval("SELECT t.audio_id FROM reading_card_tracks t JOIN reading_audio a ON a.id=t.audio_id WHERE t.card_id=$1 AND a.language_code='rus'",identifier)
    await c.execute("UPDATE telegram_chats SET audio_voice='mary' WHERE telegram_chat_id=101")
    assert await speech.refresh_prepared_tracks(c)==1
    row=await c.fetchrow("SELECT a.* FROM reading_card_tracks t JOIN reading_audio a ON a.id=t.audio_id WHERE t.card_id=$1 AND a.language_code='rus'",identifier)
    assert row['id']!=previous and row['voice_profile'].endswith(':kseniya')
    generator=AsyncMock(return_value=(b'ID3'+b'x'*700,3,'silero',row['voice_profile']))
    assert await speech.process(c,row['id'],generator=generator)=='ready'
    assert generator.await_args.args[2].russian_voice=='kseniya'
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


async def test_full_chapter_uses_card_language_without_mutating_original_or_progress(db):
    c,_,path=db
    en,_,_=await load_fixture(c,path)
    ru,_,_=await load_fixture(c,path,'rus')
    chat=await create_destination(c)
    anchor=await c.fetchrow("SELECT book_code,chapter,verse,verse_end,text FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",en['id'])
    _,original=await card(c,en,chat,anchor)
    await c.execute('UPDATE reading_cards SET selected_translation_id=$2 WHERE id=$1',original['id'],ru['id'])
    original=dict(await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1',original['id']))
    first=await message_languages.select(c,101,501,original['id'],'c',0)
    assert await message_languages.select(c,101,501,original['id'],'c',0)==first
    delivery=await c.fetchrow('SELECT * FROM delivery_log WHERE id=$1',first)
    assert delivery['translation_id']==ru['id'] and '[2]' in delivery['payload_preview']
    assert dict(await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1',original['id']))==original
    assert await c.fetchval('SELECT count(*) FROM chat_reading_progress')==0
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts')==0


async def test_legacy_frozen_excerpt_expands_to_whole_native_chapter(db):
    c,_,path=db
    edition,_,_=await load_fixture(c,path)
    await create_destination(c)
    reading,chosen=await devotionals.selected_verse(c,edition,101,date(2026,10,9),'morning_verse')
    import json
    await c.execute('UPDATE daily_verse_selections SET reading_snapshot=$2::jsonb,reading_version=1 WHERE id=$1',chosen['id'],json.dumps(reading['reading_rows'][:1]))
    refreshed,_=await devotionals.selected_verse(c,edition,101,date(2026,10,9),'morning_verse')
    assert len(refreshed['reading_rows'])==2
    after=await c.fetchrow('SELECT * FROM daily_verse_selections WHERE id=$1',chosen['id'])
    assert after['reading_version']==2 and after['text_sha256']==chosen['text_sha256']
