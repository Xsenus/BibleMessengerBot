"""Real PostgreSQL checks, executed by install.sh in an isolated disposable database.

They are SKIPPED, not counted as passed, when asyncpg or RUN_DB_TESTS=1 is absent.
No Telegram request and no real Bible download is made in these tests.
"""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import datetime,time,timezone,timedelta
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
import pytest
import pytest_asyncio

asyncpg = pytest.importorskip('asyncpg',reason='Real PostgreSQL integration requires asyncpg')
pytestmark = [pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Set RUN_DB_TESTS=1 against a PostgreSQL server to execute, not simulate, SQL checks')]

from app.config import Settings
from app.db import apply_schema
from app.catalog.importer import import_translation,_is_up_to_date
from app.catalog.models import DownloadedTranslation,VerseReference
from app.services.seed import seed_static_content
from app.services.accounts import upsert_user,upsert_chat,claim_owner
from app.services.destinations import configure_chat
from app.services.subscriptions import create_or_update_subscription,set_enabled,delete_subscriptions
from app.services.verification import verify_database
from app.services import bible
from app.worker.delivery import enqueue_next,process_delivery,prepare_subscription,recover_ambiguous,resolve_uncertain
from app.services.errors import SendError,UserError
from tests.test_hardening import metadata,OT,NT


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create and drop a uniquely named database; never DROP/TRUNCATE the configured app database."""
    original=os.environ['DATABASE_URL']
    parsed=urlsplit(original)
    name='biblebot_test_'+uuid4().hex
    dsn=urlunsplit(parsed._replace(path='/'+name))
    admin=await asyncpg.connect(original,timeout=15)
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    connection=None
    try:
        settings=replace(Settings.from_env(require_bot_token=False),database_url=dsn,bible_profile='none')
        await apply_schema(settings)
        await apply_schema(settings)  # Additive migration replay must be a no-op.
        connection=await asyncpg.connect(dsn,timeout=15)
        async with connection.transaction():
            await seed_static_content(connection)
        yield connection,settings,tmp_path
    finally:
        if connection is not None:await connection.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def load_fixture(connection,tmp_path,language='eng',long=False):
    """Synthetic 66-book, 396-chapter data solely to exercise import/index/plan mechanics."""
    refs=[];lines=[]
    for book in OT+NT:
        for chapter in range(1,7):
            for verse in range(1,3):
                refs.append(VerseReference(book,chapter,verse,len(refs)+1))
                lines.append(('SYNTHETIC NOT SCRIPTURE <A&B> 10%_ '+book+' ')*(80 if long else 1))
    raw=('\n'.join(lines)+'\n').encode()
    path=tmp_path/f'{language}-fixture.txt';path.write_bytes(raw)
    info=metadata(language_code=language,translation_id='fixture-'+language,
        ot_chapters=39*6,ot_verses=39*12,nt_chapters=27*6,nt_verses=27*12)
    downloaded=DownloadedTranslation(info,path,'https://fixture.invalid/source',hashlib.sha256(raw).hexdigest())
    result=await import_translation(connection,downloaded,refs,batch_size=73)
    return await bible.find_translation(connection,result['database_id']),downloaded,refs


async def create_destination(connection,chat_id=101,language='en'):
    """Persist a real schema destination and its owner without any Telegram API calls."""
    await upsert_user(connection,user_id=101,username=None,first_name='Fixture',last_name=None,language_code='en',default_timezone='UTC')
    await upsert_chat(connection,chat_id=chat_id,chat_type='private' if chat_id>0 else 'channel',title='Fixture',username=None,
        registered_by=101,default_timezone='UTC',ui_language=language)
    return await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat_id)


class Sender:
    """A fake transport, while all state persistence is actual PostgreSQL."""
    def __init__(self,errors=None):self.sent=[];self.errors=list(errors or [])
    async def send(self,chat_id,text,thread):
        if self.errors:
            item=self.errors.pop(0)
            if item:raise item
        self.sent.append((chat_id,text,thread))
        return len(self.sent)


async def drain(connection,identifier,sender):
    """Deliver a bounded number of fixture chunks; fail on unexpected queue state."""
    for _ in range(200):
        state=await process_delivery(connection,identifier,sender)
        if state=='sent':return
        assert state=='partial',state
    raise AssertionError('Fixture delivery did not finish')


async def test_migrations_and_real_import_indexes(db):
    connection,settings,path=db
    assert await connection.fetchval('SELECT count(*) FROM schema_migrations')==2
    edition,downloaded,refs=await load_fixture(connection,path)
    assert await _is_up_to_date(connection,downloaded)
    await import_translation(connection,downloaded,refs)
    assert await connection.fetchval('SELECT count(*) FROM verses')==792
    report=await verify_database(connection,profile='none')
    assert report['status']=='passed',report
    assert report['edition_count']==1 and report['editions'][0]['physical']['chapters']==396


async def test_destination_language_updates_all_its_modes_not_another_chat(db):
    connection,_,path=db
    en,_,_=await load_fixture(connection,path,'eng')
    ru,_,_=await load_fixture(connection,path,'rus')
    await create_destination(connection,101,'en');await create_destination(connection,-100,'en')
    for chat in (101,-100):
        await configure_chat(connection,chat,actor_id=101,translation_id=en['id'])
        for mode in ('sequential','verse_of_day'):
            await create_or_update_subscription(connection,chat_id=chat,created_by=101,translation_id=en['id'],
                mode=mode,send_time=time(9),timezone_name='UTC')
    await configure_chat(connection,-100,actor_id=101,translation_id=ru['id'],ui_language='ru')
    assert await connection.fetchval('SELECT count(*) FROM subscriptions WHERE telegram_chat_id=-100 AND translation_id=$1',ru['id'])==2
    assert await connection.fetchval('SELECT count(*) FROM subscriptions WHERE telegram_chat_id=101 AND translation_id=$1',en['id'])==2
    assert await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=101')=='en'


async def test_manual_partial_send_does_not_advance_until_final_ack(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path,long=True)
    chat=await create_destination(connection)
    chat=await configure_chat(connection,101,actor_id=101,translation_id=edition['id'])
    job=await enqueue_next(connection,chat,edition,'request-1',300)
    assert await enqueue_next(connection,chat,edition,'request-1',300)==job
    sender=Sender()
    assert await process_delivery(connection,job,sender)=='partial'
    assert await connection.fetchval('SELECT count(*) FROM chat_reading_progress')==0
    await drain(connection,job,sender)
    assert await connection.fetchval('SELECT chapter FROM chat_reading_progress WHERE telegram_chat_id=101')==1
    assert await connection.fetchval('SELECT cardinality(telegram_message_ids) FROM delivery_log WHERE id=$1',job)==len(sender.sent)


async def test_uncertain_delivery_blocks_replay_and_explicit_cancel_allows_opt_out(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path)
    chat=await create_destination(connection)
    chat=await configure_chat(connection,101,actor_id=101,translation_id=edition['id'])
    job=await enqueue_next(connection,chat,edition,'request-2')
    assert await process_delivery(connection,job,Sender([SendError('uncertain')]))=='uncertain'
    with pytest.raises(UserError):
        await configure_chat(connection,101,actor_id=101,ui_language='ru')
    await delete_subscriptions(connection,101)
    assert await connection.fetchval('SELECT status FROM delivery_log WHERE id=$1',job)=='cancelled'


async def test_pause_resume_preserves_snapshot_and_confirmed_chunks(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path,long=True)
    await create_destination(connection)
    sub=await create_or_update_subscription(connection,chat_id=101,created_by=101,translation_id=edition['id'],
        mode='sequential',send_time=time(9),timezone_name='UTC')
    await connection.execute("UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1",sub['id'])
    job=await prepare_subscription(connection,sub['id'],300)
    sender=Sender()
    assert await process_delivery(connection,job,sender)=='partial'
    await set_enabled(connection,101,False)
    assert await process_delivery(connection,job,sender)=='paused'
    await set_enabled(connection,101,True)
    assert await connection.fetchval('SELECT next_chunk FROM delivery_log WHERE id=$1',job)==1
    await drain(connection,job,sender)
    assert await connection.fetchval('SELECT current_chapter FROM subscriptions WHERE id=$1',sub['id'])==1


async def test_real_plan_day_contains_multiple_chapters_and_advances_once(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path)
    await create_destination(connection)
    sub=await create_or_update_subscription(connection,chat_id=101,created_by=101,translation_id=edition['id'],
        mode='reading_plan',send_time=time(9),timezone_name='UTC',plan_code='bible-90')
    await connection.execute("UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1",sub['id'])
    job=await prepare_subscription(connection,sub['id'],300)
    assert job
    assert await prepare_subscription(connection,sub['id'],300) is None
    await drain(connection,job,Sender())
    progress=await connection.fetchrow('SELECT plan_day,current_chapter,completed FROM subscriptions WHERE id=$1',sub['id'])
    assert progress['plan_day']==1 and progress['current_chapter']==4 and not progress['completed']


async def test_crashed_sending_is_not_treated_as_delivered(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path)
    await create_destination(connection)
    chat=await configure_chat(connection,101,actor_id=101,translation_id=edition['id'])
    job=await enqueue_next(connection,chat,edition,'request-3')
    await connection.execute("UPDATE delivery_log SET status='sending',sending_chunk=0 WHERE id=$1",job)
    assert await recover_ambiguous(connection)==1
    assert await connection.fetchval('SELECT count(*) FROM chat_reading_progress')==0
    await resolve_uncertain(connection,job,101,101,'retry-duplicate-risk')
    await drain(connection,job,Sender())
    assert await connection.fetchval('SELECT count(*) FROM chat_reading_progress')==1


async def test_claim_owner_once_and_search_wildcards(db):
    connection,_,path=db
    edition,_,_=await load_fixture(connection,path)
    await create_destination(connection)
    assert await claim_owner(connection,telegram_user_id=101,supplied_code='x'*32,expected_code='x'*32)
    assert not await claim_owner(connection,telegram_user_id=101,supplied_code='x'*32,expected_code='x'*32)
    rows=await bible.search_verses(connection,edition['id'],'10%_',3)
    assert len(rows)==3 and all('10%_' in row['text'] for row in rows)
