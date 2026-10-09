"""Shared merchant ledger with actual platform isolation and receipt delivery SQL."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.bot.native_payments import command
from app.maxbot import payments, payment_menus
from app.maxbot.identities import register, remember_message
from app.bot.ui import main_keyboard
from app.maxbot.ui import keyboard_attachment
from app.payments import ledger
from app.payments.yookassa import PaymentError
from app.worker.delivery import insert_payload, process_delivery
from tests.test_native_payment_client import CONFIG
from tests.test_postgres_integration import db as db, create_destination, Sender

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


async def test_telegram_checkout_receipt_and_history_are_separate_from_max(db,monkeypatch):
    c,_,_=db
    await create_destination(c,language='ru')
    max_user,max_chat=await register(c,user={'user_id':101},external_chat_id=303,chat_type='private',timezone='UTC')
    with pytest.raises(PaymentError):
        await ledger.new_order(c,user_id=101,chat_id=max_chat,rubles=250,method='sbp',request_key='wrong',platform='telegram')
    monkeypatch.setattr(payments.MerchantSettings,'from_env',lambda:CONFIG)
    api=SimpleNamespace(settings=CONFIG,create=AsyncMock(),payment=AsyncMock(),close=AsyncMock())
    async def create(order):
        result=dict(id=str(uuid4()),amount={'value':'250.00','currency':'RUB'},status='pending',paid=False,test=True,
                    metadata={'application':'bible-messenger-telegram','order_id':str(order['id'])},
                    recipient={'account_id':CONFIG.shop_id},payment_method={'type':'sbp'},
                    confirmation={'type':'redirect','confirmation_url':'https://yoomoney.ru/fixture'})
        api.payment.return_value=result
        return result
    api.create.side_effect=create
    monkeypatch.setattr(payments,'YooKassa',lambda config:api)
    @asynccontextmanager
    async def acquire():yield c
    pool=SimpleNamespace(acquire=acquire)
    bot=SimpleNamespace(id=777,send_message=AsyncMock())
    actor=SimpleNamespace(chat=SimpleNamespace(id=101,type='private'),from_user=SimpleNamespace(id=101,is_bot=False),message_id=55)
    await command(actor,bot,pool,arguments=('250','sbp'))
    await command(actor,bot,pool,arguments=('250','sbp'))
    assert await c.fetchval('SELECT count(*) FROM native_payment_orders')==1
    api.create.assert_awaited_once()
    order=await c.fetchrow('SELECT * FROM native_payment_orders')
    assert order['platform']=='telegram' and order['user_id']==101
    result=await create(order)
    result.update(id=order['provider_payment_id'],status='succeeded',paid=True)
    wrong=dict(result,metadata={'application':'bible-messenger-max','order_id':str(order['id'])})
    with pytest.raises(PaymentError):await ledger.record_payment(c,order['id'],wrong,CONFIG.shop_id)
    api.payment.return_value=result
    event={'type':'notification','event':'payment.succeeded','object':{'id':result['id']}}
    await ledger.notification(c,api,event)
    await ledger.notification(c,api,event)
    delivery=await c.fetchrow('SELECT * FROM delivery_log')
    assert delivery['telegram_chat_id']==101 and await c.fetchval('SELECT count(*) FROM delivery_log')==1
    chunks=json.loads(delivery['chunks'])
    assert isinstance(chunks[0],str) and '250 ₽' in chunks[0]
    sender=Sender()
    assert await process_delivery(c,delivery['id'],sender)=='sent'
    assert sender.sent[0][0]==101 and 'Спасибо' in sender.sent[0][1]
    assert await c.fetchval("SELECT count(*) FROM native_payment_orders WHERE platform='max' AND user_id=$1",max_user)==0


async def test_disabled_merchant_stale_telegram_button_cannot_create_order(db,monkeypatch):
    c,_,_=db
    await create_destination(c,language='ru')
    for key in ['YOOKASSA_SHOP_ID','YOOKASSA_SECRET_KEY','YOOKASSA_RETURN_URL','PAYMENT_SUPPORT_CONTACT']:
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('YOOKASSA_ENABLED','false')
    @asynccontextmanager
    async def acquire():yield c
    pool=SimpleNamespace(acquire=acquire)
    bot=SimpleNamespace(id=777,send_message=AsyncMock())
    actor=SimpleNamespace(chat=SimpleNamespace(id=101,type='private'),from_user=SimpleNamespace(id=101,is_bot=False),message_id=55)
    await command(actor,bot,pool,arguments=('250','sbp'))
    assert await c.fetchval('SELECT count(*) FROM native_payment_orders')==0
    assert bot.send_message.await_args.kwargs['reply_markup'] is None


async def test_delivered_max_welcome_updates_payment_button_and_preserves_image(db,monkeypatch):
    c,_,_=db
    _,chat_id=await register(c,user={'user_id':101},external_chat_id=303,chat_type='private',timezone='UTC')
    chat=await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat_id)
    monkeypatch.setattr('app.maxbot.ui.merchant_available',lambda:True)
    chunk={'kind':'max_welcome','text':'Welcome fixture','asset_sha256':'fixture',
           'max_keyboard':keyboard_attachment(main_keyboard('ru'))}
    delivery=await insert_payload(c,chat,{'id':None},chunk['text'],'fixture-menu','max_ui',{'kind':'max_ui'},frozen_chunks=[chunk])
    mid=await remember_message(c,900,chat_id,'mid.fixture')
    await c.execute("UPDATE delivery_log SET status='sent',telegram_message_ids=ARRAY[$2::bigint] WHERE id=$1",delivery,mid)
    image={'token':'fixture-image'}
    monkeypatch.setattr(payment_menus.MaxSender,'welcome_media',AsyncMock(return_value=image))
    monkeypatch.setattr(payment_menus,'wait_send_slot',AsyncMock())
    client=SimpleNamespace(edit=AsyncMock())
    monkeypatch.setattr(payment_menus,'merchant_available',lambda:False)
    assert await payment_menus.refresh(c,client,900,asyncio.Event())==1
    body=client.edit.await_args.args[1]
    assert body['attachments'][0]=={'type':'image','payload':image}
    payloads={b['payload'] for row in body['attachments'][1]['payload']['buttons'] for b in row}
    assert 'maxcmd:/donate' not in payloads and len(payloads)==7
    assert await payment_menus.refresh(c,client,900,asyncio.Event())==0
    monkeypatch.setattr(payment_menus,'merchant_available',lambda:True)
    assert await payment_menus.refresh(c,client,900,asyncio.Event())==1
    body=client.edit.await_args.args[1]
    assert body['attachments'][0]['payload']==image
    assert any(b['payload']=='maxcmd:/donate' for row in body['attachments'][1]['payload']['buttons'] for b in row)
