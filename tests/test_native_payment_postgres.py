"""Real SQL payment idempotency, notification verification and refund recovery."""
import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.maxbot.identities import register
from app.payments import ledger
from app.payments.yookassa import PaymentError
from tests.test_native_payment_client import CONFIG
from tests.test_postgres_integration import db as db

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='Real PostgreSQL required')]


@pytest_asyncio.fixture
async def context(db):
    connection,settings,_=db
    user,chat=await register(connection,user={'user_id':101,'first_name':'Fixture'},external_chat_id=303,
                             chat_type='private',timezone='UTC',locale='ru')
    order=await ledger.new_order(connection,user_id=user,chat_id=chat,rubles=250,method='sbp',request_key='fixture-key')
    result=dict(id=str(uuid4()),amount={'value':'250.00','currency':'RUB'},status='pending',paid=False,test=True,
                metadata={'application':'bible-messenger-max','order_id':str(order['id'])},
                recipient={'account_id':CONFIG.shop_id},payment_method={'type':'sbp'},
                confirmation={'type':'redirect','confirmation_url':'https://yoomoney.ru/fixture'})
    api=type('FakeMerchant',(),{})()
    api.settings=CONFIG
    api.create=AsyncMock(return_value=result)
    api.payment=AsyncMock(return_value=result)
    api.create_refund=AsyncMock()
    api.refund=AsyncMock()
    return connection,settings,user,chat,order,result,api


async def test_replayed_command_reuses_order_and_user_cannot_reuse_another_order(context):
    c,_,user,chat,order,_,_=context
    same=await ledger.new_order(c,user_id=user,chat_id=chat,rubles=250,method='sbp',request_key='fixture-key')
    assert same['id']==order['id'] and same['idempotency_key']==order['idempotency_key']
    with pytest.raises(PaymentError):
        await ledger.new_order(c,user_id=user,chat_id=chat,rubles=500,method='sbp',request_key='fixture-key')
    await c.execute('UPDATE telegram_users SET is_blocked=true WHERE telegram_user_id=$1',user)
    with pytest.raises(PaymentError):
        await ledger.new_order(c,user_id=user,chat_id=chat,rubles=250,method='sbp',request_key='other')


async def test_pending_checkout_is_not_a_receipt_and_duplicate_success_notifies_once(context):
    c,_,_,_,order,result,api=context
    pending=await ledger.checkout(c,api,order)
    assert pending['status']=='pending' and not pending['confirmed_at']
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==0
    result.update(status='succeeded',paid=True)
    await ledger.record_payment(c,order['id'],result,CONFIG.shop_id)
    await ledger.record_payment(c,order['id'],result,CONFIG.shop_id)
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==1
    assert 'ТЕСТОВЫЙ' in await c.fetchval('SELECT payload_preview FROM delivery_log')
    assert await c.fetchval('SELECT notified_at IS NOT NULL FROM native_payment_orders WHERE id=$1',order['id'])
    result.update(status='pending',paid=False)
    preserved=await ledger.record_payment(c,order['id'],result,CONFIG.shop_id)
    assert preserved['status']=='succeeded'


async def test_forged_notification_status_cannot_mark_pending_paid_and_unknown_does_not_call_api(context):
    c,_,_,_,order,result,api=context
    await ledger.checkout(c,api,order)
    event={'type':'notification','event':'payment.succeeded','object':{'id':result['id'],'status':'succeeded','paid':True}}
    assert await ledger.notification(c,api,event)
    api.payment.assert_awaited_once_with(result['id'])
    assert await c.fetchval('SELECT status FROM native_payment_orders WHERE id=$1',order['id'])=='pending'
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==0
    api.payment.reset_mock()
    event['object']['id']=str(uuid4())
    assert not await ledger.notification(c,api,event)
    api.payment.assert_not_awaited()


async def test_amount_mismatch_rolls_back_without_receipt(context):
    c,_,_,_,order,result,api=context
    await ledger.checkout(c,api,order)
    result.update(status='succeeded',paid=True,amount={'value':'251.00','currency':'RUB'})
    with pytest.raises(PaymentError):
        await ledger.record_payment(c,order['id'],result,CONFIG.shop_id)
    assert await c.fetchval('SELECT status FROM native_payment_orders WHERE id=$1',order['id'])=='pending'
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==0


async def test_lost_creation_response_reuses_key_but_never_reposts_after_dedup_window(context):
    c,_,_,_,order,_,api=context
    api.create.side_effect=PaymentError('provider_unavailable')
    with pytest.raises(PaymentError):
        await ledger.checkout(c,api,order)
    assert not await c.fetchval('SELECT provider_payment_id FROM native_payment_orders WHERE id=$1',order['id'])
    api.create.side_effect=None
    await ledger.checkout(c,api,order)
    assert api.create.await_count==2
    assert all(call.args[0]['idempotency_key']==order['idempotency_key'] for call in api.create.await_args_list)
    other=await ledger.new_order(c,user_id=order['user_id'],chat_id=order['chat_id'],rubles=100,method='bank_card',request_key='expired')
    await c.execute('UPDATE native_payment_orders SET created_at=created_at-$2::interval WHERE id=$1',other['id'],timedelta(hours=24))
    api.create.reset_mock()
    with pytest.raises(PaymentError,match='reconciliation_required'):
        await ledger.checkout(c,api,other)
    api.create.assert_not_awaited()


async def test_concurrent_checkout_creates_one_provider_payment(context):
    c,settings,_,_,order,_,api=context
    other=await asyncpg.connect(settings.database_url)
    try:
        first,second=await asyncio.gather(ledger.checkout(c,api,order),ledger.checkout(other,api,order))
        assert first['provider_payment_id']==second['provider_payment_id']
        api.create.assert_awaited_once()
        api.payment.assert_awaited_once()
    finally:
        await other.close()


async def test_refund_replay_keeps_original_receipt_and_has_one_confirmation(context):
    c,_,_,_,order,result,api=context
    result.update(status='succeeded',paid=True)
    await ledger.checkout(c,api,order)
    refund=dict(id=str(uuid4()),payment_id=result['id'],status='succeeded',amount={'value':'250.00','currency':'RUB'})
    api.create_refund.return_value=refund
    api.refund.return_value=refund
    first=await ledger.full_refund(c,api,order['id'])
    second=await ledger.full_refund(c,api,order['id'])
    assert first['provider_refund_id']==second['provider_refund_id']
    api.create_refund.assert_awaited_once()
    assert await c.fetchval('SELECT status FROM native_payment_orders WHERE id=$1',order['id'])=='succeeded'
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==2
    assert await c.fetchval('SELECT count(*) FROM native_payment_refunds')==1
    forged=dict(refund,payment_id=str(uuid4()))
    with pytest.raises(PaymentError):
        await ledger.record_refund(c,second,forged)


async def test_notification_before_create_response_commits_can_recover_known_intent(context):
    c,_,_,_,order,result,api=context
    result.update(status='succeeded',paid=True)
    event={'type':'notification','event':'payment.succeeded','object':dict(id=result['id'],metadata=result['metadata'])}
    assert await ledger.notification(c,api,event)
    assert await c.fetchval('SELECT provider_payment_id FROM native_payment_orders WHERE id=$1',order['id'])==result['id']
    assert await c.fetchval('SELECT count(*) FROM delivery_log')==1
    api.create.assert_not_awaited()
