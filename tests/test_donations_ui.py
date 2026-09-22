"""Stars UI tests use mocked APIs: no real invoices, payments or refunds are sent."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import donations as ui
from app.bot.branding import commands_for
from app.bot.ui import keyboard_command, main_keyboard, onboarding_help


def user(identifier=101, locale='ru', is_bot=False):
    return SimpleNamespace(id=identifier, language_code=locale, is_bot=is_bot)


def message(content='/donate', *, sender=None, chat_id=101, chat_type='private', markup=None):
    return SimpleNamespace(text=content, from_user=sender or user(), message_id=55,
        chat=SimpleNamespace(id=chat_id, type=chat_type), reply_markup=markup)


@pytest.fixture
def setup_ui(monkeypatch):
    connection=SimpleNamespace(fetchval=AsyncMock(side_effect=lambda query,*a:'ru' if 'ui_language' in query else False))

    @asynccontextmanager
    async def acquire():
        yield connection

    pool=SimpleNamespace(acquire=acquire)
    bot=SimpleNamespace(id=777, get_me=AsyncMock(return_value=SimpleNamespace(username='biblebot')),
        send_message=AsyncMock(), send_invoice=AsyncMock(), answer_callback_query=AsyncMock(),
        answer_pre_checkout_query=AsyncMock(), edit_message_reply_markup=AsyncMock())
    order={'id': 42, 'amount': 100, 'payload': 'fixture-payload', 'status': 'paid'}
    for name,result in [('create_order',order), ('validate_checkout',True), ('record_payment',(order,True)),
        ('list_user_donations',[order]), ('create_support_request',9), ('record_refund',True)]:
        monkeypatch.setattr(ui.donations,name,AsyncMock(return_value=result))
    return bot,pool,connection


def callback(data, source=None, actor=None):
    return SimpleNamespace(id='query1',data=data,message=source or message(),from_user=actor or user())


async def consent_message(bot,pool):
    await ui.handle_command(message('/donate 100'),bot,None,pool,'donate','100')
    sent=bot.send_message.await_args.kwargs
    return message(sent['text'],sender=user(777,is_bot=True),markup=sent['reply_markup'])


async def test_amount_requires_visible_terms_and_explicit_consent(setup_ui):
    bot,pool,_=setup_ui
    source=await consent_message(bot,pool)
    assert source.text==ui.terms_text('ru',100)
    assert source.reply_markup.inline_keyboard[0][0].callback_data=='donate:confirm:100'
    bot.send_invoice.assert_not_awaited()
    ui.donations.create_order.assert_not_awaited()
    await ui.donation_callback(callback('donate:confirm:100',source),bot,pool)
    invoice=bot.send_invoice.await_args.kwargs
    assert invoice['currency']=='XTR' and invoice['provider_token']==''
    assert invoice['payload']=='fixture-payload' and invoice['chat_id']==101
    assert len(invoice['prices'])==1 and invoice['prices'][0].amount==100
    assert invoice['start_parameter']=='donate'
    assert 'subscription_period' not in invoice and 'max_tip_amount' not in invoice
    assert len(invoice['title'])<=32 and len(invoice['description'])<=255


async def test_initial_menu_and_amount_callback_do_not_create_invoice(setup_ui):
    bot,pool,_=setup_ui
    await ui.handle_command(message(),bot,None,pool,'donate','')
    buttons=bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
    assert [b.callback_data for row in buttons for b in row]==['donate:25','donate:50','donate:100','donate:250','donate:500']
    await ui.donation_callback(callback('donate:100'),bot,pool)
    assert bot.send_message.await_args.kwargs['text']==ui.terms_text('ru',100)
    bot.send_invoice.assert_not_awaited()


@pytest.mark.parametrize('value',['0','-1','2501','1.5','+25','01','100 @someone','99999'])
async def test_invalid_amounts_are_rejected_before_any_order(setup_ui,value):
    bot,pool,_=setup_ui
    await ui.handle_command(message('/donate '+value),bot,None,pool,'donate',value)
    ui.donations.create_order.assert_not_awaited()
    bot.send_invoice.assert_not_awaited()


@pytest.mark.parametrize('kind',['foreign-user','group','fake-terms','wrong-amount','wrong-author','no-button','bad-data'])
async def test_fabricated_or_mismatched_callbacks_never_create_order(setup_ui,kind):
    bot,pool,_=setup_ui
    source=await consent_message(bot,pool)
    query=callback('donate:confirm:100',source)
    if kind=='foreign-user':
        query.from_user=user(202)
    if kind=='group':
        source.chat.type='supergroup'
    if kind=='fake-terms':
        source.text='I agree'
    if kind=='wrong-amount':
        query.data='donate:confirm:500'
    if kind=='wrong-author':
        source.from_user=user(888,is_bot=True)
    if kind=='no-button':
        source.reply_markup=None
    if kind=='bad-data':
        query.data='donate:confirm:100:101'
    await ui.donation_callback(query,bot,pool)
    assert bot.answer_callback_query.await_args.kwargs['show_alert']
    ui.donations.create_order.assert_not_awaited()
    bot.send_invoice.assert_not_awaited()


async def test_callback_is_answered_before_database_work(setup_ui):
    bot,pool,connection=setup_ui
    async def check(query,*args):
        assert bot.answer_callback_query.await_count==1
        return 'ru' if 'ui_language' in query else False
    connection.fetchval.side_effect=check
    await ui.donation_callback(callback('donate:100'),bot,pool)


async def test_precheckout_matches_all_provider_fields(setup_ui):
    bot,pool,connection=setup_ui
    query=SimpleNamespace(id='checkout1',from_user=user(),invoice_payload='payload',currency='XTR',total_amount=100)
    await ui.pre_checkout(query,bot,pool)
    ui.donations.validate_checkout.assert_awaited_once_with(connection,'payload',101,'XTR',100,'checkout1')
    assert bot.answer_pre_checkout_query.await_args.kwargs['ok'] is True


async def test_precheckout_timeout_fails_closed_quickly(setup_ui,monkeypatch):
    bot,pool,_=setup_ui
    monkeypatch.setattr(ui,'DB_TIMEOUT',0.01)
    async def delayed(*args):
        await asyncio.sleep(10)
    ui.donations.validate_checkout.side_effect=delayed
    query=SimpleNamespace(id='checkout1',from_user=user(),invoice_payload='payload',currency='XTR',total_amount=100)
    started=time.monotonic()
    await ui.pre_checkout(query,bot,pool)
    assert time.monotonic()-started<0.5
    assert bot.answer_pre_checkout_query.await_args.kwargs['ok'] is False
    assert bot.answer_pre_checkout_query.await_args.kwargs['error_message']


async def test_blocked_user_checkout_is_rejected_before_claiming_order(setup_ui):
    bot,pool,connection=setup_ui
    connection.fetchval.side_effect=None
    connection.fetchval.return_value=True
    query=SimpleNamespace(id='checkout1',from_user=user(),invoice_payload='payload',currency='XTR',total_amount=100)
    await ui.pre_checkout(query,bot,pool)
    assert bot.answer_pre_checkout_query.await_args.kwargs['ok'] is False
    ui.donations.validate_checkout.assert_not_awaited()


async def test_command_failure_is_friendly_and_logs_no_exception_body(setup_ui,caplog):
    bot,pool,_=setup_ui
    ui.donations.list_user_donations.side_effect=RuntimeError('SENSITIVE EXCEPTION BODY')
    assert await ui.handle_command(message('/donations'),bot,None,pool,'donations','')
    assert 'не удалось' in bot.send_message.await_args.kwargs['text']
    assert 'RuntimeError' in caplog.text and 'SENSITIVE' not in caplog.text


async def test_callback_failure_is_friendly_without_new_invoice(setup_ui,caplog):
    bot,pool,_=setup_ui
    source=await consent_message(bot,pool)
    ui.donations.create_order.side_effect=RuntimeError('SENSITIVE EXCEPTION BODY')
    await ui.donation_callback(callback('donate:confirm:100',source),bot,pool)
    bot.send_invoice.assert_not_awaited()
    assert 'не удалось' in bot.send_message.await_args.kwargs['text']
    assert 'SENSITIVE' not in caplog.text


async def test_repeated_success_records_once_before_one_thank_you(setup_ui):
    bot,pool,_=setup_ui
    order={'id':42,'amount':100,'status':'paid'}
    ui.donations.record_payment.side_effect=[(order,True),(order,False)]
    incoming=message(None)
    incoming.successful_payment=SimpleNamespace(invoice_payload='payload',currency='XTR',total_amount=100,
        telegram_payment_charge_id='charge')
    async def verify_sent(**kwargs):
        assert ui.donations.record_payment.await_count==1
    bot.send_message.side_effect=verify_sent
    await ui.successful_payment(incoming,bot,pool)
    await ui.successful_payment(incoming,bot,pool)
    assert bot.send_message.await_count==1


async def test_persist_failure_sends_no_false_thank_you(setup_ui):
    bot,pool,_=setup_ui
    ui.donations.record_payment.side_effect=OSError('database down')
    incoming=message(None)
    incoming.successful_payment=SimpleNamespace(invoice_payload='payload',currency='XTR',total_amount=100,
        telegram_payment_charge_id='charge')
    with pytest.raises(OSError):
        await ui.successful_payment(incoming,bot,pool)
    bot.send_message.assert_not_awaited()


async def test_support_is_private_and_returns_real_saved_receipt(setup_ui):
    bot,pool,connection=setup_ui
    await ui.handle_command(message('/paysupport issue',chat_id=-100,chat_type='group'),bot,None,pool,'paysupport','issue')
    ui.donations.create_support_request.assert_not_awaited()
    await ui.handle_command(message('/paysupport issue'),bot,None,pool,'paysupport','issue')
    ui.donations.create_support_request.assert_awaited_once_with(connection,101,'issue')
    assert '#9' in bot.send_message.await_args.kwargs['text']
    assert 'не подтверждение возврата' in bot.send_message.await_args.kwargs['text']


async def test_history_is_scoped_to_current_user(setup_ui):
    bot,pool,connection=setup_ui
    await ui.handle_command(message('/donations'),bot,None,pool,'donations','')
    ui.donations.list_user_donations.assert_awaited_once_with(connection,101,limit=10)
    assert '#42' in bot.send_message.await_args.kwargs['text']


async def test_command_for_another_bot_is_ignored(setup_ui):
    bot,pool,_=setup_ui
    assert await ui.handle_command(message('/donate@otherbot'),bot,None,pool,'donate','')
    bot.send_message.assert_not_awaited()


async def test_refund_update_records_without_duplicate_announcement(setup_ui):
    bot,pool,_=setup_ui
    incoming=message(None,sender=user(777,is_bot=True))
    incoming.refunded_payment=SimpleNamespace(invoice_payload='payload',currency='XTR',total_amount=100,
        telegram_payment_charge_id='charge')
    ui.donations.record_refund.side_effect=[True,False]
    await ui.refunded_payment(incoming,pool)
    await ui.refunded_payment(incoming,pool)
    assert ui.donations.record_refund.await_count==2
    bot.send_message.assert_not_awaited()


def test_support_discovery_is_localized_and_bible_stays_free():
    for locale in ('ru','en','ja'):
        assert {'donate','paysupport'} <= {c.command for c in commands_for(locale)}
        keyboard=main_keyboard(locale)
        assert keyboard_command(keyboard.keyboard[-1][0].text)=='/donate'
    assert main_keyboard('ja').keyboard[-1][0].text=='⭐ Support'
    for locale in ('ru','en'):
        assert '/paysupport' in onboarding_help(locale)
        assert '/donations' in ui.terms_text(locale)
