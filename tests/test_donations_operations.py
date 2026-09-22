"""Operator refunds and reconciliation use confirmed Telegram evidence only."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import RefundStarPayment
from aiogram.types import Chat, Message, StarTransaction, TransactionPartnerUser, User

from app import donations_admin
from app.services import donation_reconciliation

PAYLOAD = 'donation:v1:' + 'a' * 32
ORDER = {'id':1, 'payload':PAYLOAD, 'user_id':101, 'amount':25, 'currency':'XTR',
         'status':'paid', 'telegram_payment_charge_id':'fixture-charge'}


def connection_for(order=None):
    return SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value=order or dict(ORDER)),
                           fetchval=AsyncMock(return_value=True))


def pool_for(connection):
    @asynccontextmanager
    async def acquire():
        yield connection
    return SimpleNamespace(acquire=acquire)


def transaction(*, outgoing=False, user=101, amount=25, charge='fixture-charge',
                kind='invoice_payment', payload=PAYLOAD, nanostars=None):
    partner=TransactionPartnerUser(type='user', transaction_type=kind,
        user=User(id=user, is_bot=False, first_name='Fixture'), invoice_payload=payload)
    return StarTransaction(id=charge, amount=amount, date=datetime.now(UTC),
        nanostar_amount=nanostars, source=None if outgoing else partner,
        receiver=partner if outgoing else None)


@pytest.mark.asyncio
async def test_refund_requires_explicit_confirmation_before_any_io():
    with pytest.raises(ValueError, match='--confirm'):
        await donations_admin.refund_order(None, None, 1, confirmed=False)


@pytest.mark.asyncio
async def test_confirmed_refund_uses_exact_charge_and_records_after_api(monkeypatch):
    connection=connection_for()
    events=[]

    async def accepted(**kwargs):
        assert kwargs['user_id']==101 and kwargs['telegram_payment_charge_id']=='fixture-charge'
        events.append('telegram')
        return True

    async def saved(conn, **kwargs):
        assert conn is connection
        assert kwargs=={'payload':PAYLOAD,'currency':'XTR','total_amount':25,'charge_id':'fixture-charge'}
        events.append('ledger')
        return True

    monkeypatch.setattr(donations_admin,'record_refund',saved)
    result=await donations_admin.refund_order(connection,SimpleNamespace(refund_star_payment=accepted),1,confirmed=True)
    assert result=={'order_id':1,'status':'refunded','stars':25}
    assert events==['telegram','ledger']
    assert 'pg_advisory_unlock' in connection.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_already_recorded_refund_does_not_call_telegram_or_append(monkeypatch):
    connection=connection_for({**ORDER,'status':'refunded'})
    bot=SimpleNamespace(refund_star_payment=AsyncMock())
    record=AsyncMock()
    monkeypatch.setattr(donations_admin,'record_refund',record)
    assert (await donations_admin.refund_order(connection,bot,1,confirmed=True))['status']=='already_refunded'
    bot.refund_star_payment.assert_not_awaited()
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_already_refunded_recovers_lost_receipt(monkeypatch):
    error=TelegramBadRequest(method=RefundStarPayment(user_id=101,telegram_payment_charge_id='fixture-charge'),
                             message='Bad Request: CHARGE_ALREADY_REFUNDED')
    bot=SimpleNamespace(refund_star_payment=AsyncMock(side_effect=error))
    record=AsyncMock(return_value=True)
    monkeypatch.setattr(donations_admin,'record_refund',record)
    result=await donations_admin.refund_order(connection_for(),bot,1,confirmed=True)
    assert result['status']=='refunded'
    record.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [TimeoutError(), OSError(), TelegramBadRequest(
    method=RefundStarPayment(user_id=101,telegram_payment_charge_id='fixture-charge'), message='CHARGE_NOT_FOUND')])
async def test_ambiguous_or_rejected_refund_never_changes_ledger(monkeypatch,error):
    connection=connection_for()
    record=AsyncMock()
    monkeypatch.setattr(donations_admin,'record_refund',record)
    with pytest.raises(type(error)):
        await donations_admin.refund_order(connection,SimpleNamespace(refund_star_payment=AsyncMock(side_effect=error)),1,confirmed=True)
    record.assert_not_awaited()
    assert 'pg_advisory_unlock' in connection.execute.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [None,False,1])
async def test_only_explicit_true_refund_result_can_append_ledger(monkeypatch,accepted):
    record=AsyncMock()
    monkeypatch.setattr(donations_admin,'record_refund',record)
    with pytest.raises(RuntimeError,match='did not confirm'):
        await donations_admin.refund_order(connection_for(),SimpleNamespace(refund_star_payment=AsyncMock(return_value=accepted)),1,confirmed=True)
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_passes_all_payment_bindings_and_deduplicates_totals(monkeypatch):
    connection=connection_for()
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(transactions=[transaction()])))
    record=AsyncMock(side_effect=[(ORDER,True),(ORDER,False)])
    refund=AsyncMock()
    monkeypatch.setattr(donation_reconciliation,'record_payment',record)
    monkeypatch.setattr(donation_reconciliation,'record_refund',refund)
    first=await donation_reconciliation.reconcile_page(bot,pool_for(connection),100)
    second=await donation_reconciliation.reconcile_page(bot,pool_for(connection),100)
    assert first=={'scanned':1,'payments':1,'refunds':0,'rejected':0}
    assert second=={'scanned':1,'payments':0,'refunds':0,'rejected':0}
    assert record.await_args.kwargs=={'payload':PAYLOAD,'user_id':101,'currency':'XTR','total_amount':25,'charge_id':'fixture-charge'}
    bot.get_star_transactions.assert_awaited_with(offset=100,limit=100,request_timeout=15)
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_reconciliation_recovers_payment_before_refund_and_counts_once(monkeypatch):
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(transactions=[transaction(outgoing=True)])))
    events=[]

    async def record_payment(connection,**kwargs):
        assert kwargs['charge_id']=='fixture-charge' and kwargs['total_amount']==25
        newly='payment' not in events
        events.append('payment')
        return ORDER,newly

    async def record_refund(connection,**kwargs):
        assert events[-1]=='payment'
        assert kwargs=={'payload':PAYLOAD,'currency':'XTR','total_amount':25,'charge_id':'fixture-charge'}
        newly='refund' not in events
        events.append('refund')
        return newly

    monkeypatch.setattr(donation_reconciliation,'record_payment',record_payment)
    monkeypatch.setattr(donation_reconciliation,'record_refund',record_refund)
    pool=pool_for(connection_for())
    assert await donation_reconciliation.reconcile_page(bot,pool)=={'scanned':1,'payments':1,'refunds':1,'rejected':0}
    assert await donation_reconciliation.reconcile_page(bot,pool)=={'scanned':1,'payments':0,'refunds':0,'rejected':0}
    assert events==['payment','refund','payment','refund']


@pytest.mark.asyncio
@pytest.mark.parametrize('item', [transaction(kind='gift_purchase'),transaction(payload=None),
    transaction(amount=0),transaction(nanostars=1),
    StarTransaction(id='other',amount=25,date=datetime.now(UTC))])
async def test_non_invoice_or_fractional_transactions_never_enter_ledger(monkeypatch,item):
    record=AsyncMock()
    monkeypatch.setattr(donation_reconciliation,'record_payment',record)
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(transactions=[item])))
    connection=connection_for()
    connection.fetchval.return_value=None
    assert await donation_reconciliation.reconcile_page(bot,pool_for(connection))=={'scanned':1,'payments':0,'refunds':0,'rejected':0}
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_payload_belongs_to_no_donation_ledger(monkeypatch):
    connection=connection_for()
    connection.fetchval.return_value=False
    record=AsyncMock()
    monkeypatch.setattr(donation_reconciliation,'record_payment',record)
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(transactions=[transaction()])))
    assert (await donation_reconciliation.reconcile_page(bot,pool_for(connection)))['payments']==0
    record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('item', [transaction(user=202),transaction(amount=26),transaction(outgoing=True,user=202)])
async def test_matching_payload_cannot_bypass_domain_binding_checks(monkeypatch,item):
    record=AsyncMock(side_effect=ValueError('fixture mismatch'))
    refund=AsyncMock()
    monkeypatch.setattr(donation_reconciliation,'record_payment',record)
    monkeypatch.setattr(donation_reconciliation,'record_refund',refund)
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(transactions=[item])))
    result=await donation_reconciliation.reconcile_page(bot,pool_for(connection_for()))
    assert result=={'scanned':1,'payments':0,'refunds':0,'rejected':1}
    record.assert_awaited_once()
    refund.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('user,accepted', [(101,True),(202,False)])
async def test_refund_without_payload_uses_known_charge_and_still_validates_user(monkeypatch,user,accepted):
    connection=connection_for()

    async def fetchval(sql,*args):
        if 'SELECT payload FROM donation_payments' in sql:
            assert args==('fixture-charge',)
            return PAYLOAD
        return True

    connection.fetchval=fetchval
    record=AsyncMock(return_value=(ORDER,False),side_effect=None if accepted else ValueError('wrong user'))
    refund=AsyncMock(return_value=True)
    monkeypatch.setattr(donation_reconciliation,'record_payment',record)
    monkeypatch.setattr(donation_reconciliation,'record_refund',refund)
    bot=SimpleNamespace(get_star_transactions=AsyncMock(return_value=SimpleNamespace(
        transactions=[transaction(outgoing=True,user=user,payload=None)])))
    result=await donation_reconciliation.reconcile_page(bot,pool_for(connection))
    assert result=={'scanned':1,'payments':0,'refunds':int(accepted),'rejected':int(not accepted)}
    assert record.await_args.kwargs['user_id']==user and record.await_args.kwargs['payload']==PAYLOAD
    assert refund.await_count==int(accepted)


@pytest.mark.asyncio
async def test_uncertain_support_reply_is_not_marked_answered():
    connection=connection_for({'id':1,'user_id':101,'status':'open'})
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        await donations_admin.reply_support(connection,bot,1,'Fixture reply')
    assert all('UPDATE donation_support_requests' not in call.args[0] for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_closed_support_ticket_cannot_send_another_reply():
    connection=connection_for({'id':1,'user_id':101,'status':'closed'})
    bot=SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(ValueError,match='already closed'):
        await donations_admin.reply_support(connection,bot,1,'Fixture reply')
    bot.send_message.assert_not_awaited()


class SingleLeasePool:
    """Catch nested leases even when payment error handling suppresses the exception."""
    def __init__(self, connection):
        self.connection=connection
        self.active=False
        self.violations=0
        self.acquired=0

    @asynccontextmanager
    async def acquire(self):
        if self.active:
            self.violations+=1
            raise AssertionError('Nested connection lease')
        self.active=True
        self.acquired+=1
        try:
            yield self.connection
        finally:
            self.active=False


def private_message(value):
    return Message(message_id=17,date=datetime.now(UTC),chat=Chat(id=101,type='private'),
        from_user=User(id=101,is_bot=False,first_name='Fixture',language_code='ru'),text=value)


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['command','deep-link','keyboard'])
async def test_normal_handler_delegates_donations_after_releasing_its_connection(route):
    from app.bot import handlers, ui

    async def fetchval(sql,*args):
        return False if 'is_blocked' in sql else 'ru'

    connection=SimpleNamespace(execute=AsyncMock(),fetchval=fetchval)
    pool=SingleLeasePool(connection)
    bot=SimpleNamespace(send_message=AsyncMock())
    settings=SimpleNamespace(default_timezone='UTC',max_message_length=3900)
    if route=='keyboard':
        label=next(button.text for row in ui.main_keyboard('ru').keyboard for button in row
                   if ui.keyboard_command(button.text)=='/donate')
        await handlers.private_text_handler(private_message(label),bot,pool,settings)
    else:
        await handlers.command_handler(private_message('/start donate' if route=='deep-link' else '/donate'),bot,pool,settings)
    assert pool.violations==0 and pool.acquired>=2 and not pool.active
    bot.send_message.assert_awaited_once()
    assert 'donate:' in str(bot.send_message.await_args.kwargs['reply_markup'])


@pytest.mark.asyncio
async def test_blocked_user_can_still_open_payment_support_without_nested_lease(monkeypatch):
    from app.bot import handlers
    from app.services import donations

    async def fetchval(sql,*args):
        return True if 'is_blocked' in sql else 'ru'

    connection=SimpleNamespace(execute=AsyncMock(),fetchval=fetchval)
    pool=SingleLeasePool(connection)
    bot=SimpleNamespace(send_message=AsyncMock())
    support=AsyncMock(return_value=123)
    monkeypatch.setattr(donations,'create_support_request',support)
    await handlers.command_handler(private_message('/paysupport Payment issue'),bot,pool,
        SimpleNamespace(default_timezone='UTC',max_message_length=3900))
    support.assert_awaited_once_with(connection,101,'Payment issue')
    assert pool.violations==0 and not pool.active
    assert '#123' in bot.send_message.await_args.kwargs['text']
