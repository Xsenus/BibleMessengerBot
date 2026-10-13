"""Администраторы на любой платформе: приглашение владельцем, одноразовый код, снятие прав."""
import os

import pytest

from app.bot import admin
from app.services import accounts
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


async def users(c):
    await create_destination(c, 101)
    await c.execute("UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101")
    # администратор из другой платформы (MAX) и обычный пользователь
    await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone,first_name) VALUES(-4000000000000001,'max','UTC','Анна'),(303,'telegram','UTC','Пётр')")
    return await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')


@pytest.mark.asyncio
async def test_invite_is_single_use_and_works_for_any_platform(db):
    c = db[0]
    chat = await users(c)
    code = await accounts.create_admin_invite(c, 101)
    assert await accounts.redeem_admin_invite(c, -4000000000000001, code) is True
    assert await accounts.is_admin(c, -4000000000000001)
    assert await accounts.redeem_admin_invite(c, 303, code) is False          # код сгорел
    assert not await accounts.is_admin(c, 303)
    assert await accounts.redeem_admin_invite(c, 303, 'wrong') is False
    with pytest.raises(UserError):                                            # пригласить может только владелец
        await accounts.create_admin_invite(c, -4000000000000001)
    text, _ = await admin.command(c, -4000000000000001, dict(chat), ['list'])
    assert 'Анна' in text and 'max' in text


@pytest.mark.asyncio
async def test_expired_invite_rejected_and_removal_owner_only(db):
    c = db[0]
    chat = await users(c)
    code = await accounts.create_admin_invite(c, 101)
    await c.execute("UPDATE admin_invites SET expires_at=now()-interval '1 minute'")
    assert await accounts.redeem_admin_invite(c, 303, code) is False
    code = await accounts.create_admin_invite(c, 101)
    await accounts.redeem_admin_invite(c, 303, code)
    with pytest.raises(UserError):
        await accounts.remove_admin(c, 303, 303)
    assert await accounts.remove_admin(c, 101, 303) is True
    assert not await accounts.is_admin(c, 303)
    assert await accounts.remove_admin(c, 101, 101) is False                   # владельца снять нельзя
    with pytest.raises(UserError):
        await admin.command(c, 303, dict(chat), [])                            # обычному пользователю меню недоступно
    text, markup = await admin.callback(c, 101, dict(chat), 'home')
    assert 'panel' in text.lower() and markup.inline_keyboard
