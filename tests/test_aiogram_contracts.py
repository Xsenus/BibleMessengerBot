"""Actual aiogram import/model/authorization tests. No Telegram network calls."""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime,timezone
import pytest

pytest.importorskip('aiogram',reason='aiogram is unavailable in this audit environment')
from aiogram.types import Chat,User,Message
from app.bot.handlers import authorize,help_text,settings_keyboard,enum_value
from app.bot.main import COMMAND_KEYS
from app.services.errors import UserError
from app.services.i18n import available_ui
from app.services.formatting import split_message
from aiogram.types import BotCommand


@pytest.mark.parametrize('locale',sorted(available_ui()))
def test_real_aiogram_models_and_localized_markup(locale):
    chat={'telegram_chat_id':-100123,'ui_language':locale}
    markup=settings_keyboard(chat)
    assert markup.inline_keyboard
    assert all(len(button.callback_data.encode())<=64 for row in markup.inline_keyboard for button in row)
    assert split_message(help_text(locale))
    from app.services.i18n import tr
    for command,key in COMMAND_KEYS:
        assert BotCommand(command=command,description=tr(locale,key))


@pytest.mark.asyncio
async def test_channel_admin_must_have_post_permission():
    chat=Chat(id=-100123,type='channel',title='Fixture')
    actor=SimpleNamespace(status='administrator')
    bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
        get_chat_member=AsyncMock(side_effect=[actor,SimpleNamespace(status='administrator',can_post_messages=False)]))
    with pytest.raises(UserError,match='forbidden'):
        await authorize(bot,chat,101)


@pytest.mark.asyncio
async def test_private_destination_cannot_be_another_user():
    with pytest.raises(UserError):
        await authorize(None,Chat(id=222,type='private'),101)


@pytest.mark.asyncio
async def test_non_admin_cannot_change_group_language():
    bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
        get_chat_member=AsyncMock(side_effect=[SimpleNamespace(status='member'),SimpleNamespace(status='administrator')]))
    with pytest.raises(UserError):
        await authorize(bot,Chat(id=-100,type='supergroup',title='Fixture'),101)


def test_real_message_chat_type_is_normalized():
    message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=101,type='private'),
        from_user=User(id=101,is_bot=False,first_name='Fixture'),text='/settings')
    assert enum_value(message.chat.type)=='private'
