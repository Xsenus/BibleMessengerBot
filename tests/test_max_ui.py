"""Shared menus preserve command meaning when mapped to MAX inline buttons."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ui import main_keyboard
from app.maxbot.ui import keyboard_attachment


def test_main_navigation_has_all_eight_commands_in_both_languages():
    for locale in ['ru', 'en']:
        result = keyboard_attachment(main_keyboard(locale))
        payloads = {b['payload'] for row in result['payload']['buttons'] for b in row}
        assert payloads == {'maxcmd:/next', 'maxcmd:/today', 'maxcmd:/random', 'maxcmd:/search',
                            'maxcmd:/settings', 'maxcmd:/help', 'maxcmd:/daily', 'maxcmd:/donate'}


def test_card_callbacks_preserve_bound_card_and_audio_is_separate_action():
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='English', callback_data='lc:12:s:34')]])
    result = keyboard_attachment(markup, audio=(12, 56))
    assert result['payload']['buttons'] == [
        [{'type': 'callback', 'text': 'English', 'payload': 'lc:12:s:34'}],
        [{'type': 'callback', 'text': '🔊 Слушать', 'payload': 'maxaudio:12:56'}],
    ]
