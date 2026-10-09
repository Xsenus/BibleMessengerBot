"""Shared menus preserve command meaning when mapped to MAX inline buttons."""
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers import command_target_suffix
from app.bot.ui import main_keyboard
from app.maxbot.ui import keyboard_attachment, payment_controls


@pytest.mark.parametrize('enabled',[False,True])
def test_main_navigation_matches_merchant_availability_in_both_languages(monkeypatch,enabled):
    monkeypatch.setattr('app.maxbot.ui.merchant_available',lambda:enabled)
    for locale in ['ru', 'en']:
        result = keyboard_attachment(main_keyboard(locale))
        payloads = {b['payload'] for row in result['payload']['buttons'] for b in row}
        expected = {'maxcmd:/next', 'maxcmd:/today', 'maxcmd:/random', 'maxcmd:/search',
                    'maxcmd:/settings', 'maxcmd:/help', 'maxcmd:/daily'}
        assert payloads == expected | ({'maxcmd:/donate'} if enabled else set())


def test_stale_navigation_can_remove_and_restore_payment_without_losing_other_buttons(monkeypatch):
    monkeypatch.setattr('app.maxbot.ui.merchant_available',lambda:True)
    original=keyboard_attachment(main_keyboard('en'))
    hidden=payment_controls(original,enabled=False)
    restored=payment_controls(hidden,enabled=True,locale='en')
    assert sum(len(row) for row in original['payload']['buttons'])==8
    assert sum(len(row) for row in hidden['payload']['buttons'])==7
    assert sum(len(row) for row in restored['payload']['buttons'])==8
    assert restored['payload']['buttons'][-1][0]['text']=='💳 Support'
    assert payment_controls({'type':'inline_keyboard','payload':{'buttons':[[
        {'type':'callback','text':'Pay','payload':'maxpay:100:sbp'}]]}},enabled=False) is None


def test_card_callbacks_preserve_bound_card_and_audio_is_separate_action():
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='English', callback_data='lc:12:s:34')]])
    result = keyboard_attachment(markup, audio=(12, 56))
    assert result['payload']['buttons'] == [
        [{'type': 'callback', 'text': 'English', 'payload': 'lc:12:s:34'}],
        [{'type': 'callback', 'text': '🔊 Слушать', 'payload': 'maxaudio:12:56'}],
    ]


def test_private_reading_shortcuts_keep_bound_language_and_audio_buttons():
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='English', callback_data='lc:12:s:34')]])
    result = keyboard_attachment(markup, audio=(12, 56), navigation=True)
    payloads = [b['payload'] for row in result['payload']['buttons'] for b in row]
    assert payloads == ['lc:12:s:34','maxcmd:/next','maxcmd:/random','maxcmd:/search','maxcmd:/menu','maxaudio:12:56']
    assert all(len(row) <= 3 for row in result['payload']['buttons'])


@pytest.mark.asyncio
@pytest.mark.parametrize('platform,kind,identifier,expected',[
    ('telegram','private',101,''),('telegram','channel',-404,' -404'),
    ('max','private',-1000000001,''),('max','channel',-1000000002,' chat:404'),
])
async def test_copyable_targets_keep_telegram_syntax_and_hide_max_internal_ids(platform,kind,identifier,expected):
    connection=AsyncMock()
    connection.fetchval.return_value=404
    assert await command_target_suffix(connection,{
        'platform':platform,'chat_type':kind,'telegram_chat_id':identifier,
    })==expected
    if platform=='max' and kind!='private':
        connection.fetchval.assert_awaited_once()
    else:
        connection.fetchval.assert_not_awaited()
