"""Complete MAX commands/cards/outbox with real isolated PostgreSQL and fake REST."""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.maxbot.bridge import MaxBotBridge
from app.maxbot.client import MaxClient
from app.maxbot.config import MaxSettings
from app.maxbot.dispatcher import MaxDispatcher
from app.maxbot.events import parse_event
from app.maxbot.identities import lookup, message_external
from app.maxbot.inbox import due_events, process, recover
from app.services.platform_sender import PlatformTransports
from app.worker.delivery import process_delivery
from tests.test_max_events import fixture_event
from tests.test_postgres_integration import db as db
from tests.test_postgres_integration import load_fixture

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


@pytest_asyncio.fixture
async def max_context(db,monkeypatch):
    connection,settings,path=db
    monkeypatch.setenv('AUDIO_ENABLED','false')
    await load_fixture(connection,path)
    pool=await asyncpg.create_pool(settings.database_url,min_size=1,max_size=8)
    requests=[]
    sent=[]
    permissions={'actor_admin':True,'bot_write':True}

    def handle(request):
        requests.append(request)
        if request.url.path=='/me':
            return httpx.Response(200,json={'user_id':900,'is_bot':True,'first_name':'Fixture Bible','username':'fixture_bot'})
        if request.url.path=='/chats/404':
            return httpx.Response(200,json={'chat_id':404,'type':'channel','status':'active','title':'Fixture channel'})
        if request.url.path=='/chats/404/members/me':
            return httpx.Response(200,json={'user_id':900,'is_admin':True,'permissions':['write'] if permissions['bot_write'] else []})
        if request.url.path=='/chats/404/members':
            return httpx.Response(200,json={'members':[{'user_id':101,'is_admin':permissions['actor_admin'],'permissions':['write']}]})
        if request.url.path=='/uploads':
            if request.url.params.get('type')=='audio':
                return httpx.Response(200,json={'url':'https://omu.okcdn.ru/upload','token':'fixture-audio'})
            return httpx.Response(200,json={'url':'https://iu.oneme.ru/uploadImage?apiToken=fixture'})
        if request.url.host=='omu.okcdn.ru':
            return httpx.Response(200,text='<retval>1</retval>')
        if request.url.host=='iu.oneme.ru':
            return httpx.Response(200,json={'photos':{'123':{'token':'fixture-photo'}}})
        if request.url.path=='/answers' or request.method=='PUT':
            return httpx.Response(200,json={'success':True})
        if request.url.path=='/messages' and request.method=='POST':
            mid=f'mid.fixture-{len(sent)+1}'
            body=json.loads(request.content)
            sent.append((mid,body))
            return httpx.Response(200,json={'message':{'body':{'mid':mid}}})
        raise AssertionError(f'Unexpected MAX request {request.method} {request.url.path}')

    client=MaxClient(MaxSettings(token='fixture-token-only'),transport=httpx.MockTransport(handle))
    bridge=MaxBotBridge(client,pool,{'user_id':900,'is_bot':True,'first_name':'Fixture Bible','username':'fixture_bot'})
    dispatcher=MaxDispatcher(bridge,pool,settings)
    dispatcher.fixture_permissions=permissions
    transports=PlatformTransports(None,settings,max_client=client)
    try:
        yield connection,dispatcher,transports,requests,sent,path
    finally:
        await transports.close()
        await pool.close()


async def command(context,text,sequence=1):
    _,dispatcher,_,_,_,_=context
    event=fixture_event()
    event['message']['body'].update(mid=f'mid.incoming-{sequence}',text=text)
    key,_=parse_event(json.dumps(event).encode())
    await dispatcher.dispatch(event,key)


async def drain(context):
    connection,_,transports,_,_,_=context
    for _ in range(50):
        identifier=await connection.fetchval("SELECT id FROM delivery_log WHERE status='pending' ORDER BY id LIMIT 1")
        if identifier is None:
            return
        result=await process_delivery(connection,identifier,transports.sender(connection))
        assert result in {'partial','sent'},result
    raise AssertionError('MAX fixture queue did not drain')


async def test_start_and_random_use_durable_max_cards_without_telegram(max_context):
    c,_,_,requests,sent,_=max_context
    await command(max_context,'/start')
    assert not sent
    await drain(max_context)
    assert sent[0][1]['attachments'][0]['type']=='image'
    buttons=sent[0][1]['attachments'][1]['payload']['buttons']
    assert any(button.get('payload')=='maxcmd:/random' for row in buttons for button in row)
    await command(max_context,'/random',2)
    await drain(max_context)
    card=await c.fetchrow('SELECT * FROM reading_cards')
    assert card['platform']=='max' and card['telegram_message_id']>0
    mid=await message_external(c,900,card['telegram_chat_id'],card['telegram_message_id'])
    assert mid==sent[-1][0]
    assert all(request.url.host!='api.telegram.org' for request in requests)


async def test_translation_callback_edits_exact_original_and_keeps_chat_preferences(max_context):
    c,dispatcher,_,requests,sent,path=max_context
    second,_,_=await load_fixture(c,path,language='rus')
    await command(max_context,'/random')
    await drain(max_context)
    card=await c.fetchrow('SELECT * FROM reading_cards')
    old_preference=await c.fetchval('SELECT default_translation_id FROM telegram_chats WHERE telegram_chat_id=$1',card['telegram_chat_id'])
    original={'sender':{'user_id':900,'is_bot':True,'first_name':'Fixture Bible'},
              'recipient':{'chat_id':303,'chat_type':'dialog'},'body':{'mid':sent[-1][0],'text':'Fixture'}}
    event={'update_type':'message_callback','timestamp':456,'user_locale':'en','message':original,
           'callback':{'callback_id':'fixture-callback','user':{'user_id':101,'first_name':'Fixture'},
                       'payload':f"lc:{card['id']}:s:{second['id']}"}}
    await dispatcher.dispatch(event,'fixture-translation-event')
    await drain(max_context)
    updates=[r for r in requests if r.method=='PUT']
    assert len(updates)==1 and updates[0].url.params['message_id']==original['body']['mid']
    assert len(sent)==1
    assert await c.fetchval('SELECT selected_translation_id FROM reading_cards WHERE id=$1',card['id'])==second['id']
    assert await c.fetchval('SELECT default_translation_id FROM telegram_chats WHERE telegram_chat_id=$1',card['telegram_chat_id'])==old_preference


async def test_daily_devotions_timezone_pause_resume_and_search(max_context):
    c,_,_,_,sent,_=max_context
    commands=['/daily','/devotions','/timezone Europe/Moscow','/pause','/resume','/read Genesis 1:1',
              '/search SYNTHETIC','/translations','/language en','/ui en','/status','/license']
    for sequence,text in enumerate(commands,1):
        await command(max_context,text,sequence)
        await drain(max_context)
    chat=await lookup(c,'chat',303)
    rows=await c.fetch('SELECT * FROM subscriptions WHERE telegram_chat_id=$1',chat)
    assert {r['mode'] for r in rows}=={'verse_of_day','morning_verse','evening_verse'}
    assert all(r['is_enabled'] and r['timezone']=='Europe/Moscow' for r in rows)
    assert {r['send_time'].strftime('%H:%M') for r in rows if r['mode']!='verse_of_day'}=={'09:38','21:13'}
    assert len(sent)>=len(commands)
    assert any('SYNTHETIC' in (body.get('text') or '') for _,body in sent)
    assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE status IN ('failed','uncertain'))")


async def test_inbox_recovery_never_executes_interrupted_event_again(max_context):
    c,_,_,_,_,_=max_context
    identifier=await c.fetchval("INSERT INTO max_inbox(event_key,payload,chat_key,status) VALUES('fixture','{}','303','processing') RETURNING id")
    await recover(c)
    dispatcher=type('FakeDispatcher',(),{'dispatch':AsyncMock()})()
    pool=type('FixturePool',(),{})()
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def acquire():
        yield c
    pool.acquire=acquire
    assert await process(pool,dispatcher,identifier)=='stale'
    dispatcher.dispatch.assert_not_awaited()
    assert await c.fetchval('SELECT status FROM max_inbox WHERE id=$1',identifier)=='uncertain'


async def test_due_events_selects_only_first_pending_event_per_free_chat(max_context):
    c,_,_,_,_,_=max_context
    for index,chat in enumerate(['303','303','304','305'],1):
        await c.execute("INSERT INTO max_inbox(event_key,payload,chat_key) VALUES($1,'{}',$2)",f'fixture-{index}',chat)
    rows=await due_events(c,('305',))
    assert [row['chat_key'] for row in rows]==['303','304']
    await c.execute("UPDATE max_inbox SET status='processing' WHERE event_key='fixture-1'")
    assert [row['chat_key'] for row in await due_events(c)]==['304','305']


async def test_positive_max_channel_target_and_revoked_rights_fail_closed(max_context):
    c,dispatcher,_,_,sent,_=max_context
    await dispatcher.dispatch({'update_type':'bot_added','timestamp':456,'chat_id':404,'is_channel':True,
                               'user':{'user_id':101,'first_name':'Fixture'}},'fixture-add-channel')
    await command(max_context,'/subscribe sequential 09:00 UTC chat:404')
    await drain(max_context)
    channel=await lookup(c,'chat',404)
    assert await c.fetchval('SELECT count(*) FROM subscriptions WHERE telegram_chat_id=$1',channel)==1
    await command(max_context,'/settings chat:404',2)
    await drain(max_context)
    assert any('<code>404</code>' in (body.get('text') or '') for _,body in sent)
    dispatcher.fixture_permissions['actor_admin']=False
    await command(max_context,'/time 10:00 UTC chat:404',3)
    await drain(max_context)
    assert await c.fetchval('SELECT send_time::text FROM subscriptions WHERE telegram_chat_id=$1',channel)=='09:00:00'
    dispatcher.fixture_permissions['bot_write']=False
    await dispatcher.dispatch({'update_type':'bot_admin_permissions_changed','timestamp':456,'chat_id':404},'fixture-rights-revoked')
    assert not await c.fetchval('SELECT is_active FROM telegram_chats WHERE telegram_chat_id=$1',channel)
    assert not await c.fetchval('SELECT is_enabled FROM subscriptions WHERE telegram_chat_id=$1',channel)


async def test_time_help_uses_copyable_max_destination_for_command_and_button(max_context):
    c,dispatcher,_,_,sent,_=max_context
    await command(max_context,'/start')
    await drain(max_context)
    private=await lookup(c,'chat',303)
    await dispatcher.dispatch({'update_type':'bot_added','timestamp':456,'chat_id':404,'is_channel':True,
                               'user':{'user_id':101,'first_name':'Fixture'}},'fixture-time-channel')
    channel=await lookup(c,'chat',404)
    original={'sender':{'user_id':900,'is_bot':True,'first_name':'Fixture Bible'},
              'recipient':{'chat_id':303,'chat_type':'dialog'},'body':{'mid':sent[0][0],'text':'Fixture'}}
    for index,(target,suffix) in enumerate([(private,''),(channel,' chat:404')],1):
        await command(max_context,'/time'+suffix,index+1)
        await drain(max_context)
        zone=await c.fetchval('SELECT timezone FROM telegram_chats WHERE telegram_chat_id=$1',target)
        expected=f'<code>/time 09:00 {zone}{suffix}</code>'
        assert expected in sent[-1][1]['text']
        assert str(target) not in sent[-1][1]['text']
        event={'update_type':'message_callback','timestamp':456,'user_locale':'en','message':original,
               'callback':{'callback_id':f'fixture-time-{index}','user':{'user_id':101,'first_name':'Fixture'},
                           'payload':f'v1:timehelp:{target}:'}}
        await dispatcher.dispatch(event,f'fixture-time-event-{index}')
        await drain(max_context)
        assert expected in sent[-1][1]['text']
        assert str(target) not in sent[-1][1]['text']
    # The group suffix shown in a private management dialog can be pasted back.
    await command(max_context,f'/subscribe sequential 09:00 {zone} chat:404',4)
    await drain(max_context)
    await command(max_context,f'/time 10:00 {zone} chat:404',5)
    await drain(max_context)
    assert await c.fetchval('SELECT timezone FROM telegram_chats WHERE telegram_chat_id=$1',channel)==zone
    assert await c.fetchval('SELECT send_time::text FROM subscriptions WHERE telegram_chat_id=$1',channel)=='10:00:00'


async def test_commands_addressed_to_other_bots_never_trigger_max_intercepts(max_context):
    c,_,_,requests,sent,_=max_context
    for index,name in enumerate(['help','chats','thread','donate','donations','paysupport','terms','refund'],1):
        await command(max_context,f'/{name}@other_fixture_bot',index)
    await command(max_context,'/settings@other_fixture_bot chat:999999',19)
    assert not requests and not sent
    assert not await c.fetchval('SELECT EXISTS(SELECT 1 FROM delivery_log)')
    assert not await c.fetchval('SELECT EXISTS(SELECT 1 FROM native_payment_orders)')
    assert not await c.fetchval('SELECT EXISTS(SELECT 1 FROM native_payment_support)')
    # Case-insensitive own-bot mentions must still work.
    await command(max_context,'/help@FIXTURE_BOT',20)
    await drain(max_context)
    assert len(sent)==1 and '/next' in sent[0][1]['text']


async def test_two_next_clicks_on_same_menu_have_independent_acknowledged_progress(max_context):
    c,dispatcher,_,_,sent,_=max_context
    await command(max_context,'/start')
    await drain(max_context)
    original={'sender':{'user_id':900,'is_bot':True,'first_name':'Fixture Bible'},
              'recipient':{'chat_id':303,'chat_type':'dialog'},'body':{'mid':sent[0][0],'text':'Fixture'}}
    for number in (1,2):
        event={'update_type':'message_callback','timestamp':456,'user_locale':'en','message':original,
               'callback':{'callback_id':f'fixture-click-{number}','user':{'user_id':101,'first_name':'Fixture'},
                           'payload':'maxcmd:/next'}}
        await dispatcher.dispatch(event,f'fixture-click-event-{number}')
        await drain(max_context)
    assert len(sent)==3
    assert 'Genesis 1' in sent[1][1]['text'] and 'Genesis 2' in sent[2][1]['text']
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='manual' AND status='sent'")==2


async def test_native_support_menu_sbp_checkout_confirmation_and_private_ticket(max_context,monkeypatch):
    from uuid import uuid4

    from app.maxbot import payments
    from app.payments import ledger
    from tests.test_native_payment_client import CONFIG
    c,_,_,_,sent,_=max_context
    for key,value in [('YOOKASSA_SHOP_ID',CONFIG.shop_id),('YOOKASSA_SECRET_KEY',CONFIG.secret_key),
                      ('YOOKASSA_RETURN_URL',CONFIG.return_url),('PAYMENT_SUPPORT_CONTACT',CONFIG.support_contact)]:
        monkeypatch.setenv(key,value)
    api=type('FakeMerchant',(),{})()
    api.settings=CONFIG
    results=[]
    async def create(order):
        result=dict(id=str(uuid4()),amount={'value':'250.00','currency':'RUB'},status='pending',paid=False,test=True,
                    metadata={'application':'bible-messenger-max','order_id':str(order['id'])},
                    recipient={'account_id':CONFIG.shop_id},payment_method={'type':'sbp'},
                    confirmation={'type':'redirect','confirmation_url':'https://yoomoney.ru/fixture'})
        results.append(result)
        return result
    api.create=AsyncMock(side_effect=create)
    api.close=AsyncMock()
    monkeypatch.setattr(payments,'YooKassa',lambda _:api)
    await command(max_context,'/donate')
    await drain(max_context)
    assert 'Stars' not in sent[-1][1]['text']
    rows=sent[-1][1]['attachments'][0]['payload']['buttons']
    assert rows[0][1]['payload']=='maxpay:100:sbp'
    await command(max_context,'/donate 250 sbp',2)
    await drain(max_context)
    assert sent[-1][1]['attachments'][0]['payload']['buttons'][0][0]['url']=='https://yoomoney.ru/fixture'
    assert await c.fetchval('SELECT status FROM native_payment_orders')=='pending'
    results[0].update(status='succeeded',paid=True)
    order_id=await c.fetchval('SELECT id FROM native_payment_orders')
    await ledger.record_payment(c,order_id,results[0],CONFIG.shop_id)
    await drain(max_context)
    assert 'TEST PAYMENT' in sent[-1][1]['text']
    await command(max_context,'/donations',3)
    await drain(max_context)
    assert str(order_id) in sent[-1][1]['text']
    await command(max_context,'/paysupport Refund please',4)
    await drain(max_context)
    assert await c.fetchval('SELECT message FROM native_payment_support')=='Refund please'
    assert all(str(request.url.host) != 'api.telegram.org' for request in max_context[3])


async def test_late_audio_offers_one_button_then_sends_standalone_cached_mp3(max_context,monkeypatch):
    from app.services import speech
    c,dispatcher,_,requests,sent,_=max_context
    monkeypatch.setenv('AUDIO_ENABLED','true')
    await command(max_context,'/read Genesis 1:1')
    await drain(max_context)
    card=await c.fetchrow('SELECT * FROM reading_cards')
    assert card['audio_id'] and not card['audio_offered_id'] and not card['audio_sent_id']
    assert await speech.process(c,card['audio_id'],generator=AsyncMock(return_value=(b'ID3'+b'a'*1000,10,'fixture','fixture-voice')))=='ready'
    assert await speech.dispatch(c)==1
    await drain(max_context)
    assert await speech.dispatch(c)==0
    updates=[request for request in requests if request.method=='PUT']
    assert len(updates)==1 and updates[0].url.params['message_id']==sent[0][0]
    body=json.loads(updates[0].content)
    buttons=body['attachments'][0]['payload']['buttons']
    assert buttons[-1][0]['payload']==f"maxaudio:{card['id']}:{card['audio_id']}"
    assert len(sent)==1
    original={'sender':{'user_id':900,'is_bot':True,'first_name':'Fixture Bible'},
              'recipient':{'chat_id':303,'chat_type':'dialog'},'body':{'mid':sent[0][0],'text':'Fixture'}}
    event={'update_type':'message_callback','timestamp':456,'user_locale':'en','message':original,
           'callback':{'callback_id':'fixture-listen','user':{'user_id':101,'first_name':'Fixture'},
                       'payload':buttons[-1][0]['payload']}}
    await dispatcher.dispatch(event,'fixture-listen-event')
    await drain(max_context)
    assert len(sent)==2
    response=sent[-1][1]
    assert response['attachments'][0]=={'type':'audio','payload':{'token':'fixture-audio'}}
    assert response['link']=={'type':'reply','mid':sent[0][0]}
    assert 'Audio reading' in response['text'] and '1:1' in response['text']
    shortcuts={b['payload'] for row in response['attachments'][1]['payload']['buttons'] for b in row}
    assert shortcuts=={'maxcmd:/next','maxcmd:/random','maxcmd:/search','maxcmd:/menu'}
    updated=await c.fetchrow('SELECT * FROM reading_cards')
    assert updated['audio_offered_id']==updated['audio_sent_id']==updated['audio_id']
    assert await c.fetchval("SELECT count(*) FROM max_media WHERE kind='audio'")==1
    original['body']['mid']='mid.other-original'
    event['callback']['callback_id']='fixture-forged-listen'
    await dispatcher.dispatch(event,'fixture-forged-listen-event')
    await drain(max_context)
    assert len([body for _,body in sent if any(a['type']=='audio' for a in body.get('attachments',[]))])==1


async def test_prayer_prepared_early_uses_max_queue_and_local_schedule_without_zone_line(max_context,monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.services import prayers
    c,_,_,_,sent,_=max_context
    await command(max_context,'/devotions')
    await drain(max_context)
    chat=await lookup(c,'chat',303)
    await command(max_context,'/timezone Europe/Moscow',2)
    await drain(max_context)
    clock=datetime(2026,10,10,6,20,tzinfo=UTC)
    monkeypatch.setattr(prayers,'brief',AsyncMock(return_value={'prayer_text':'Fixture prayer.',
                          'generator':'template:fixture','news_snapshot':[]}))
    await prayers.prepare_due(c,now=clock)
    row=await c.fetchrow("SELECT d.* FROM delivery_log d WHERE d.telegram_chat_id=$1 AND mode='prayer'",chat)
    assert row and row['status']=='pending' and row['scheduled_for']==clock+timedelta(minutes=18)
    assert 'Asia/' not in row['payload_preview'] and 'Europe/' not in row['payload_preview']
    assert '09:38' not in row['payload_preview']
    await c.execute("UPDATE delivery_log SET scheduled_for=now()-interval '1 second' WHERE id=$1",row['id'])
    await drain(max_context)
    assert 'Fixture prayer.' in sent[-1][1]['text']
