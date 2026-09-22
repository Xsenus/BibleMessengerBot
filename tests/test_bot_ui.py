"""Native navigation exercises the existing authorization and command contracts."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import CallbackQuery, Chat, FSInputFile, Message, ReplyKeyboardMarkup, User

from app.bot import handlers
from app.bot.commands import encode_callback, parse_command
from app.bot.ui import BUTTONS, keyboard_command, main_keyboard, search_prompt, welcome_text
from app.services.formatting import split_message
from app.services.i18n import available_ui


def message(text, *, chat_id=101, chat_type='private', is_bot=False):
    return Message(message_id=7, date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type, title='Fixture' if chat_id < 0 else None),
        from_user=User(id=101, is_bot=is_bot, first_name='Fixture', language_code='ru'), text=text)


def pool_for(connection):
    @asynccontextmanager
    async def acquire():
        yield connection
    return SimpleNamespace(acquire=Mock(side_effect=acquire))


@pytest.mark.parametrize('locale', sorted(available_ui()))
def test_every_localized_keyboard_has_seven_working_commands(locale):
    keyboard = main_keyboard(locale)
    assert keyboard.resize_keyboard and keyboard.is_persistent and not keyboard.one_time_keyboard
    assert [len(row) for row in keyboard.keyboard] == [2, 2, 2, 1]
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert [keyboard_command(label) for label in labels] == ['/' + name for name, _ in BUTTONS]
    assert len(set(labels)) == 7
    assert split_message(welcome_text(locale, 'Fixture <Edition> & text'))
    assert split_message(search_prompt(locale))
    assert split_message(handlers.help_text(locale))


@pytest.mark.asyncio
@pytest.mark.parametrize('locale', ['ru', 'en'])
async def test_keyboard_labels_route_through_authorized_command_handler(monkeypatch, locale):
    command_handler = AsyncMock()
    monkeypatch.setattr(handlers, 'command_handler', command_handler)
    for row in main_keyboard(locale).keyboard:
        for button in row:
            await handlers.private_text_handler(message(button.text), None, None, None)
            routed = command_handler.await_args.args[0]
            assert routed.text == keyboard_command(button.text)
            assert routed.chat.id == 101 and routed.from_user.id == 101 and routed.message_id == 7


@pytest.mark.asyncio
async def test_unknown_text_is_helpful_only_in_private_chat(monkeypatch):
    command_handler = AsyncMock()
    monkeypatch.setattr(handlers, 'command_handler', command_handler)
    await handlers.private_text_handler(message('Здравствуйте'), None, None, None)
    assert command_handler.await_args.args[0].text == '/menu'
    command_handler.reset_mock()
    await handlers.private_text_handler(message('Здравствуйте', chat_id=-100, chat_type='supergroup'), None, None, None)
    command_handler.assert_not_awaited()
    assert keyboard_command('⚙️ Настройки -100') is None


@pytest.mark.asyncio
async def test_blocked_user_cannot_use_keyboard_to_run_actions(monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value=True))
    run = AsyncMock()
    monkeypatch.setattr(handlers, 'run_command', run)
    response = AsyncMock()
    monkeypatch.setattr(handlers, 'reply', response)
    await handlers.private_text_handler(message(main_keyboard('ru').keyboard[0][0].text), None,
        pool_for(connection), SimpleNamespace(default_timezone='UTC'))
    run.assert_not_awaited()
    assert response.await_count == 1


@pytest.mark.asyncio
async def test_bot_messages_cannot_activate_keyboard_actions(monkeypatch):
    pool = SimpleNamespace(acquire=Mock())
    await handlers.private_text_handler(message('📖 Читать дальше', is_bot=True), None, pool, None)
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('command,expected', [('/search', '/search любовь'), ('/time', '/time 09:00 UTC')])
async def test_prompt_commands_work_without_telegram_requests(monkeypatch, command, expected):
    chat = {'telegram_chat_id': 101, 'ui_language': 'ru', 'timezone': 'UTC'}
    connection = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value=chat))
    bot = SimpleNamespace(get_chat=AsyncMock(), get_me=AsyncMock(), get_chat_member=AsyncMock())
    monkeypatch.setattr(handlers, 'upsert_chat', AsyncMock())
    text, markup = await handlers.run_command(connection, bot, SimpleNamespace(default_timezone='UTC'),
        message(command), parse_command(command))
    assert expected in text and markup is None
    bot.get_chat.assert_not_awaited()
    bot.get_me.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_returns_one_welcome_with_persistent_keyboard(monkeypatch):
    chat = {'telegram_chat_id': 101, 'ui_language': 'ru', 'timezone': 'UTC'}
    monkeypatch.setattr(handlers, 'destination', AsyncMock(return_value=chat))
    monkeypatch.setattr(handlers.bible, 'chat_translation', AsyncMock(return_value={'title': '<Edition> & source'}))
    text, markup = await handlers.run_command(None, None, None, message('/start'), parse_command('/start'))
    assert isinstance(markup, ReplyKeyboardMarkup) and markup.is_persistent
    assert '&lt;Edition&gt; &amp; source' in text
    assert len(split_message(text)) == 1


@pytest.mark.asyncio
async def test_forged_settings_callback_cannot_change_unauthorized_group(monkeypatch):
    callback = CallbackQuery(id='fixture', chat_instance='fixture',
        from_user=User(id=101, is_bot=False, first_name='Fixture', language_code='ru'),
        data=encode_callback('ui', -100, 'en'), message=message('Settings'))
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=Chat(id=-100, type='supergroup', title='Fixture')),
        get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
        get_chat_member=AsyncMock(side_effect=[SimpleNamespace(status='member'), SimpleNamespace(status='administrator')]),
    )
    connection = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(handlers, 'register_context', AsyncMock())
    configure = AsyncMock()
    monkeypatch.setattr(handlers, 'configure_chat', configure)
    monkeypatch.setattr(handlers, 'reply', AsyncMock())
    monkeypatch.setattr(CallbackQuery, 'answer', AsyncMock())
    await handlers.callback_handler(callback, bot, pool_for(connection), SimpleNamespace(default_timezone='UTC'))
    configure.assert_not_awaited()
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_settings_callback_avoids_unnecessary_get_chat(monkeypatch):
    chat = message('/settings').chat
    connection = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value={'telegram_chat_id':101}))
    bot = SimpleNamespace(get_chat=AsyncMock(), get_me=AsyncMock(), get_chat_member=AsyncMock())
    monkeypatch.setattr(handlers, 'upsert_chat', AsyncMock())
    await handlers.destination(connection, bot, chat, 101, 101, SimpleNamespace(default_timezone='UTC'))
    bot.get_chat.assert_not_awaited()
    bot.get_me.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_welcome_upload_cached_file_id_reused_with_shared_rate_limit(tmp_path, monkeypatch):
    from app.bot import ui
    image = tmp_path / 'welcome.png'
    image.write_bytes(b'fixture image bytes')
    connection = SimpleNamespace(fetchval=AsyncMock(side_effect=[None, 'cached-photo']), execute=AsyncMock())
    bot = SimpleNamespace(id=777, send_photo=AsyncMock(return_value=SimpleNamespace(
        photo=[SimpleNamespace(file_id='small'), SimpleNamespace(file_id='cached-photo')])) )
    limiter = AsyncMock()
    monkeypatch.setattr(ui, 'wait_send_slot', limiter)
    settings = SimpleNamespace(telegram_global_rate_per_second=20, telegram_chat_rate_per_second=1)
    for _ in range(2):
        assert await ui.send_welcome(bot, connection, settings, 101, ui.welcome_text('ru', 'Fixture'),
            main_keyboard('ru'), image_path=image)
    assert isinstance(bot.send_photo.await_args_list[0].kwargs['photo'], FSInputFile)
    assert bot.send_photo.await_args_list[1].kwargs['photo'] == 'cached-photo'
    assert connection.execute.await_count == 1 and limiter.await_count == 2
    assert connection.execute.await_args.args[1].startswith('welcome:777:')
    assert connection.execute.await_args.args[2] == 'cached-photo'


@pytest.mark.asyncio
async def test_photo_failure_requests_text_fallback(tmp_path, monkeypatch):
    from app.bot import ui
    image = tmp_path / 'welcome.png'
    image.write_bytes(b'fixture')
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=None), execute=AsyncMock())
    bot = SimpleNamespace(id=777, send_photo=AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(ui, 'wait_send_slot', AsyncMock())
    settings = SimpleNamespace(telegram_global_rate_per_second=20, telegram_chat_rate_per_second=1)
    assert not await ui.send_welcome(bot, connection, settings, 101, 'Welcome', main_keyboard('en'), image_path=image)
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_cache_failure_does_not_duplicate_delivered_photo(tmp_path, monkeypatch):
    from app.bot import ui
    image = tmp_path / 'welcome.png'
    image.write_bytes(b'fixture')
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=None), execute=AsyncMock(side_effect=OSError()))
    bot = SimpleNamespace(id=777, send_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='file')])) )
    monkeypatch.setattr(ui, 'wait_send_slot', AsyncMock())
    settings = SimpleNamespace(telegram_global_rate_per_second=20, telegram_chat_rate_per_second=1)
    assert await ui.send_welcome(bot, connection, settings, 101, 'Welcome', main_keyboard('en'), image_path=image)


@pytest.mark.asyncio
@pytest.mark.parametrize('command,chat_id,chat_type,photo_ok,photos,replies', [
    ('/start',101,'private',True,1,0),
    ('/start',101,'private',False,1,1),
    ('/menu',101,'private',True,0,1),
    ('/start',-100,'supergroup',True,0,1),
])
async def test_welcome_photo_only_on_private_start_with_one_response(monkeypatch, command, chat_id, chat_type, photo_ok, photos, replies):
    connection = SimpleNamespace(fetchval=AsyncMock(return_value='ru'))
    monkeypatch.setattr(handlers, 'register_context', AsyncMock())
    monkeypatch.setattr(handlers, 'run_command', AsyncMock(return_value=('Welcome', main_keyboard('ru'))))
    photo = AsyncMock(return_value=photo_ok)
    reply = AsyncMock()
    monkeypatch.setattr(handlers, 'send_welcome', photo)
    monkeypatch.setattr(handlers, 'reply', reply)
    await handlers.command_handler(message(command, chat_id=chat_id, chat_type=chat_type), None,
        pool_for(connection), None)
    assert photo.await_count == photos and reply.await_count == replies
