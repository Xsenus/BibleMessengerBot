"""Conditional UI and callback ownership; no merchant or Telegram network requests."""
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import donations, native_payments
from app.payments.yookassa import MerchantSettings, merchant_available
from tests.test_donations_ui import setup_ui as setup_ui, message, user, callback


@pytest.mark.parametrize('enabled',[False,True])
async def test_telegram_preserves_stars_and_only_offers_cards_when_available(setup_ui,monkeypatch,enabled):
    bot,pool,_=setup_ui
    monkeypatch.setattr(donations,'merchant_available',lambda:enabled)
    await donations.handle_command(message(),bot,None,pool,'donate','')
    buttons={b.callback_data for row in bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row}
    assert buttons=={'donate:25','donate:50','donate:100','donate:250','donate:500'}|({'rubmenu'} if enabled else set())
    bot.send_invoice.assert_not_awaited()


@pytest.mark.parametrize('failure',['foreign-actor','foreign-bot','unoffered','group'])
async def test_native_callback_requires_own_private_bot_menu(setup_ui,monkeypatch,failure):
    bot,pool,_=setup_ui
    checkout=AsyncMock()
    monkeypatch.setattr(native_payments.payments,'checkout',checkout)
    markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='250 ₽',callback_data='rubpay:250:sbp')]])
    source=message(sender=user(777,is_bot=True),markup=markup)
    actor=user()
    if failure=='foreign-actor':actor=user(202)
    if failure=='foreign-bot':source.from_user=user(888,is_bot=True)
    if failure=='unoffered':source.reply_markup=None
    if failure=='group':source.chat.type='group'
    await native_payments.callback(callback('rubpay:250:sbp',source,actor),bot,pool)
    checkout.assert_not_awaited()
    bot.answer_callback_query.assert_awaited_once()


async def test_native_adapter_does_not_replace_stars_commands(setup_ui,monkeypatch):
    bot,pool,_=setup_ui
    support=AsyncMock()
    monkeypatch.setattr(native_payments.payments,'support',support)
    await donations.handle_command(message('/paysupport card Payment issue'),bot,None,pool,'paysupport','card Payment issue')
    adapter,actor,parsed=support.await_args.args
    assert adapter.bridge.platform=='telegram' and parsed.arguments==('Payment','issue')
    await adapter._response(actor.chat,'/terms · /paysupport')
    assert bot.send_message.await_args.kwargs['text']=='/terms card · /paysupport card'


def test_explicit_disabled_flag_wins_over_valid_credentials(monkeypatch):
    for name,value in {'YOOKASSA_SHOP_ID':'12345','YOOKASSA_SECRET_KEY':'fixture-only',
                       'YOOKASSA_RETURN_URL':'https://example.invalid/return','PAYMENT_SUPPORT_CONTACT':'fixture',
                       'YOOKASSA_ENABLED':'false'}.items():monkeypatch.setenv(name,value)
    assert not MerchantSettings.from_env().enabled and not merchant_available()
    monkeypatch.setenv('YOOKASSA_ENABLED','true')
    assert merchant_available()
    monkeypatch.delenv('YOOKASSA_SECRET_KEY')
    assert not merchant_available()


@pytest.mark.parametrize('enabled',[False,True])
def test_max_slash_commands_and_help_do_not_advertise_disabled_merchant(monkeypatch,enabled):
    from app.maxbot import main,help
    monkeypatch.setattr(main,'merchant_available',lambda:enabled)
    monkeypatch.setattr('app.payments.yookassa.merchant_available',lambda:enabled)
    commands={item['name'] for item in main.command_menu()}
    assert ('donate' in commands)==enabled and ('paysupport' in commands)==enabled
    assert ('/donate' in help.help_text('ru'))==enabled
    assert ('/donate' in help.help_text('en'))==enabled
