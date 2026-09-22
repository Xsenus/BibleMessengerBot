"""Real PostgreSQL transaction/concurrency tests for the Stars ledger."""
from __future__ import annotations

import asyncio
import os
import secrets

import pytest

from app.services import donations
from tests.test_postgres_integration import db as db

asyncpg = pytest.importorskip('asyncpg')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


def payment(order, charge_id='fixture-charge'):
    return {'payload':order['payload'], 'user_id':order['user_id'], 'currency':'XTR',
            'total_amount':order['amount'], 'charge_id':charge_id}


def refund(order, charge_id='fixture-charge'):
    return {'payload':order['payload'], 'currency':'XTR', 'total_amount':order['amount'], 'charge_id':charge_id}


async def expired_order(connection):
    return await connection.fetchrow('''INSERT INTO donation_orders(payload,user_id,amount,created_at,expires_at)
        VALUES($1,101,25,now()-interval '2 hours',now()-interval '1 hour') RETURNING *''',
        'donation:v1:' + secrets.token_urlsafe(24))


async def test_order_checkout_duplicate_and_authoritative_receipt(db):
    connection,_,_=db
    order=await donations.create_order(connection,101,25)
    assert order['status']=='pending' and order['currency']=='XTR'
    assert (order['expires_at']-order['created_at']).total_seconds()==3600
    assert await connection.fetchval('SELECT count(*) FROM telegram_users')==0
    assert await donations.validate_checkout(connection,order['payload'],101,'XTR',25,'query-1')
    assert await donations.validate_checkout(connection,order['payload'],101,'XTR',25,'query-1')
    assert not await donations.validate_checkout(connection,order['payload'],101,'XTR',25,'query-2')
    assert await donations.list_user_donations(connection,101)==[]
    saved,new=await donations.record_payment(connection,**payment(order))
    assert new and saved['status']=='paid' and saved['telegram_payment_charge_id']=='fixture-charge'
    again,new=await donations.record_payment(connection,**payment(order))
    assert not new and again['paid_at']==saved['paid_at']
    assert not await donations.validate_checkout(connection,order['payload'],101,'XTR',25,'query-1')
    assert await connection.fetchval('SELECT count(*) FROM donation_payments')==1


async def test_confirmed_payment_survives_expiry_and_missing_precheckout(db):
    connection,_,_=db
    order=await expired_order(connection)
    assert not await donations.validate_checkout(connection,order['payload'],101,'XTR',25,'late-query')
    saved,new=await donations.record_payment(connection,**payment(order))
    assert new and saved['status']=='paid' and saved['checkout_query_id'] is None
    fresh=await donations.create_order(connection,101,50)
    saved,new=await donations.record_payment(connection,**payment(fresh,'no-checkout-charge'))
    assert new and saved['status']=='paid'


@pytest.mark.parametrize('field,value', [('user_id',202),('currency','USD'),('total_amount',26),
    ('payload','donation:v1:'+'z'*32)])
async def test_mismatched_payment_never_changes_order_or_ledger(db,field,value):
    connection,_,_=db
    order=await donations.create_order(connection,101,25)
    data=payment(order)
    data[field]=value
    with pytest.raises(ValueError):
        await donations.record_payment(connection,**data)
    assert await connection.fetchval('SELECT count(*) FROM donation_payments')==0
    assert await connection.fetchval('SELECT status FROM donation_orders WHERE id=$1',order['id'])=='pending'


async def test_checkout_cannot_switch_user_currency_amount_or_reuse_query_for_other_order(db):
    connection,_,_=db
    first=await donations.create_order(connection,101,25)
    second=await donations.create_order(connection,101,25)
    for user,currency,amount in [(202,'XTR',25),(101,'USD',25),(101,'XTR',26)]:
        assert not await donations.validate_checkout(connection,first['payload'],user,currency,amount,'same-query')
    assert await donations.validate_checkout(connection,first['payload'],101,'XTR',25,'same-query')
    assert not await donations.validate_checkout(connection,second['payload'],101,'XTR',25,'same-query')


async def test_concurrent_distinct_checkouts_reserve_only_one_query(db):
    connection,settings,_=db
    order=await donations.create_order(connection,101,25)
    peer=await asyncpg.connect(settings.database_url)
    try:
        outcomes=await asyncio.gather(
            donations.validate_checkout(connection,order['payload'],101,'XTR',25,'query-a'),
            donations.validate_checkout(peer,order['payload'],101,'XTR',25,'query-b'))
        assert sorted(outcomes)==[False,True]
        winner='query-a' if outcomes[0] else 'query-b'
        assert await donations.validate_checkout(connection,order['payload'],101,'XTR',25,winner)
    finally:
        await peer.close()


async def test_concurrent_duplicate_receipts_are_recorded_once(db):
    connection,settings,_=db
    order=await donations.create_order(connection,101,25)
    peer=await asyncpg.connect(settings.database_url)
    try:
        outcomes=await asyncio.gather(donations.record_payment(connection,**payment(order)),
                                      donations.record_payment(peer,**payment(order)))
        assert sorted(item[1] for item in outcomes)==[False,True]
        assert await connection.fetchval('SELECT count(*) FROM donation_payments')==1
    finally:
        await peer.close()


async def test_charge_id_and_payload_cannot_be_reassigned(db):
    connection,_,_=db
    first=await donations.create_order(connection,101,25)
    second=await donations.create_order(connection,202,25)
    await donations.record_payment(connection,**payment(first))
    with pytest.raises(ValueError):
        await donations.record_payment(connection,**payment(second))
    with pytest.raises(ValueError):
        await donations.record_payment(connection,**payment(first,'different-charge'))
    assert await connection.fetchval('SELECT count(*) FROM donation_payments')==1


async def test_refund_is_idempotent_preserves_original_receipt_and_history_is_private(db):
    connection,_,_=db
    first=await donations.create_order(connection,101,25)
    second=await donations.create_order(connection,202,50)
    await donations.record_payment(connection,**payment(first))
    await donations.record_payment(connection,**payment(second,'other-charge'))
    original=await connection.fetchrow('SELECT * FROM donation_payments WHERE order_id=$1',first['id'])
    assert await donations.record_refund(connection,**refund(first))
    assert not await donations.record_refund(connection,**refund(first))
    saved,new=await donations.record_payment(connection,**payment(first))
    assert not new and saved['status']=='refunded' and saved['refunded_at']
    assert await connection.fetchrow('SELECT * FROM donation_payments WHERE order_id=$1',first['id'])==original
    history=await donations.list_user_donations(connection,101)
    assert len(history)==1 and history[0]['id']==first['id'] and history[0]['status']=='refunded'
    assert len(await donations.list_user_donations(connection,202))==1
    assert await donations.list_user_donations(connection,303)==[]


@pytest.mark.parametrize('field,value', [('currency','USD'),('total_amount',26),('charge_id','wrong'),
    ('payload','donation:v1:'+'z'*32)])
async def test_mismatched_refund_never_changes_receipt(db,field,value):
    connection,_,_=db
    order=await donations.create_order(connection,101,25)
    await donations.record_payment(connection,**payment(order))
    data=refund(order)
    data[field]=value
    with pytest.raises(ValueError):
        await donations.record_refund(connection,**data)
    assert await connection.fetchval('SELECT count(*) FROM donation_refunds')==0
    assert await connection.fetchval('SELECT status FROM donation_orders WHERE id=$1',order['id'])=='paid'


async def test_sql_ledger_and_order_identity_cannot_be_overwritten_or_deleted(db):
    connection,_,_=db
    order=await donations.create_order(connection,101,25)
    await donations.record_payment(connection,**payment(order))
    await donations.record_refund(connection,**refund(order))
    for sql in ("UPDATE donation_orders SET amount=50", "UPDATE donation_orders SET user_id=202",
                "UPDATE donation_payments SET amount=50", "DELETE FROM donation_payments",
                "UPDATE donation_refunds SET amount=50", "DELETE FROM donation_refunds"):
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await connection.execute(sql)
    assert await connection.fetchval('SELECT amount FROM donation_payments')==25
    assert await connection.fetchval('SELECT count(*) FROM donation_refunds')==1


async def test_support_ticket_is_private_database_record_without_user_registration(db):
    connection,_,_=db
    identifier=await donations.create_support_request(connection,303,'  Private payment issue\nReceipt #1  ')
    row=await connection.fetchrow('SELECT * FROM donation_support_requests WHERE id=$1',identifier)
    assert row['user_id']==303 and row['message']=='Private payment issue\nReceipt #1' and row['status']=='open'
    assert row['reply_text'] is None and row['replied_at'] is None
    assert await connection.fetchval('SELECT count(*) FROM telegram_users')==0
