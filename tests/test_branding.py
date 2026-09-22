"""Telegram profile contracts, retry isolation and persistent avatar idempotence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SetMyName
from aiogram.types import InputProfilePhotoStatic, MenuButtonCommands

from app.bot import branding


class SettingsStore:
    def __init__(self):
        self.values = {}

    async def fetchval(self,query,key):
        return self.values.get(key)

    async def execute(self,query,key,value):
        self.values[key] = value


@pytest.fixture
def setup_branding(tmp_path,monkeypatch):
    monkeypatch.setattr(branding,'available_ui',lambda:{'ru':'Русский','en':'English'})
    bot = SimpleNamespace(id=123456)
    for method in ('set_my_name','set_my_description','set_my_short_description',
                   'set_my_commands','set_chat_menu_button','set_my_profile_photo'):
        setattr(bot,method,AsyncMock(return_value=True))
    avatar = tmp_path/'avatar.png'
    avatar.write_bytes(b'synthetic avatar upload fixture; not a production image')
    return bot,SettingsStore(),avatar


def test_profiles_and_command_lists_meet_real_api_limits():
    for profile in branding.PROFILE.values():
        assert 1<=len(profile['name'])<=64
        assert 1<=len(profile['description'])<=512
        assert 1<=len(profile['short_description'])<=120
    for locale in ['',*branding.available_ui()]:
        commands = branding.commands_for(locale)
        assert len(commands)<=100 and len({c.command for c in commands})==len(commands)
        assert all(1<=len(c.description)<=256 for c in commands)
        assert {'search','language','ui','time','license','help'} <= {c.command for c in commands}
        assert not {'reset','claim','resolve','topics'} & {c.command for c in commands}
    assert branding.commands_for('')[0].description=='Открыть главное меню'
    assert branding.commands_for('en')[0].description=='Open the main menu'


async def test_profile_menu_and_avatar_are_applied_once(setup_branding):
    bot,connection,avatar = setup_branding
    assert await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_name.await_count==3
    assert bot.set_my_description.await_count==3
    assert bot.set_my_short_description.await_count==3
    assert bot.set_my_commands.await_count==3
    assert isinstance(bot.set_chat_menu_button.await_args.kwargs['menu_button'],MenuButtonCommands)
    photo = bot.set_my_profile_photo.await_args.kwargs['photo']
    assert isinstance(photo,InputProfilePhotoStatic) and photo.photo.path==avatar
    assert await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_profile_photo.await_count==1
    assert bot.set_my_commands.await_count==3
    avatar.write_bytes(b'new synthetic avatar revision')
    assert await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_profile_photo.await_count==2
    assert bot.set_my_name.await_count==3


async def test_optional_avatar_missing_does_not_prevent_profile_and_menu(setup_branding):
    bot,connection,avatar = setup_branding
    avatar.unlink()
    assert not await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_description.await_count==3
    assert bot.set_chat_menu_button.await_count==1
    bot.set_my_profile_photo.assert_not_awaited()


async def test_failed_api_update_is_not_marked_done_and_retries(setup_branding):
    bot,connection,avatar = setup_branding
    bot.set_my_profile_photo.side_effect=TelegramNetworkError(method=SetMyName(name='fixture'),message='network down')
    assert not await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert f'branding:{bot.id}:avatar' not in connection.values
    bot.set_my_profile_photo.side_effect=None
    assert await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_profile_photo.await_count==2
    assert bot.set_my_description.await_count==3


async def test_bot_identity_is_part_of_the_fingerprint_key(setup_branding):
    bot,connection,avatar = setup_branding
    await branding.apply_branding(bot,connection,avatar_path=avatar)
    bot.id = 654321
    await branding.apply_branding(bot,connection,avatar_path=avatar)
    assert bot.set_my_profile_photo.await_count==2
    assert bot.set_my_name.await_count==6


async def test_background_loop_respects_telegram_retry_after(monkeypatch):
    @asynccontextmanager
    async def acquire():
        yield SettingsStore()
    pool = SimpleNamespace(acquire=acquire)
    limited=TelegramRetryAfter(method=SetMyName(name='fixture'),message='limited',retry_after=700)
    monkeypatch.setattr(branding,'apply_branding',AsyncMock(side_effect=limited))
    sleep=AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(branding.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):
        await branding.branding_loop(SimpleNamespace(),pool)
    sleep.assert_awaited_once_with(700)


async def test_background_db_failure_is_retried_without_crashing(monkeypatch):
    @asynccontextmanager
    async def acquire():
        raise OSError('synthetic database connection outage')
        yield
    sleep=AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(branding.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):
        await branding.branding_loop(SimpleNamespace(),SimpleNamespace(acquire=acquire))
    sleep.assert_awaited_once_with(300)
