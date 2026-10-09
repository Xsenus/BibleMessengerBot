"""Durable MAX identity mapping; equal external IDs never alias Telegram accounts."""
from __future__ import annotations

from typing import Any

from app.services.accounts import upsert_chat, upsert_user
from app.services.errors import UserError
from app.services.locks import lock_key


def valid_external_id(value: object) -> int:
    if type(value) is not int or not -(2**63) < value < 2**63 or value == 0:
        raise ValueError('Invalid MAX identifier')
    return value


async def lookup(connection: Any, kind: str, external_id: int) -> int | None:
    return await connection.fetchval(
        "SELECT internal_id FROM platform_identities WHERE platform='max' AND kind=$1 AND external_id=$2",
        kind, valid_external_id(external_id),
    )


async def external(connection: Any, kind: str, internal_id: int) -> int:
    value = await connection.fetchval(
        "SELECT external_id FROM platform_identities WHERE platform='max' AND kind=$1 AND internal_id=$2",
        kind, internal_id,
    )
    if value is None:
        raise UserError('forbidden')
    return value


async def _allocate(connection: Any, kind: str, external_id: int,
                    *, shared_id: int | None = None) -> int:
    if kind not in {'user', 'chat'}:
        raise ValueError('Invalid identity kind')
    valid_external_id(external_id)
    # Called under the global MAX mapping lock. Check actual existing identities,
    # rather than assuming the reserved sequence cannot collide with legacy IDs.
    previous = await lookup(connection, kind, external_id)
    if previous is not None:
        if shared_id is not None and previous != shared_id:
            raise UserError('forbidden')
        return previous
    identifier = shared_id
    while identifier is None:
        candidate = await connection.fetchval("SELECT nextval('platform_identity_seq')")
        used = await connection.fetchval('''SELECT EXISTS(
            SELECT 1 FROM telegram_users WHERE telegram_user_id=$1
            UNION ALL SELECT 1 FROM telegram_chats WHERE telegram_chat_id=$1
            UNION ALL SELECT 1 FROM platform_identities WHERE internal_id=$1)''', candidate)
        if not used:
            identifier = candidate
    await connection.execute('''INSERT INTO platform_identities(platform,kind,external_id,internal_id)
        VALUES('max',$1,$2,$3)''', kind, external_id, identifier)
    return identifier


async def register(connection: Any, *, user: dict, external_chat_id: int, chat_type: str,
                   timezone: str, locale: str = 'ru', title: str | None = None) -> tuple[int, int]:
    """Atomically map a verified MAX update and initialize independent preferences.

    A private chat deliberately has the actor's internal ID, preserving the shared
    authorization invariant even though MAX's external dialog ID is different.
    """
    if chat_type not in {'private', 'group', 'channel'}:
        raise ValueError('Invalid MAX chat type')
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key('max-identities', 1))
        actor = await _allocate(connection, 'user', valid_external_id(user.get('user_id')))
        chat = await _allocate(connection, 'chat', external_chat_id,
                               shared_id=actor if chat_type == 'private' else None)
        await upsert_user(connection, user_id=actor, username=user.get('username'),
                          first_name=user.get('first_name'), last_name=user.get('last_name'),
                          language_code=locale, default_timezone=timezone)
        await connection.execute("UPDATE telegram_users SET platform='max' WHERE telegram_user_id=$1", actor)
        await upsert_chat(connection, chat_id=chat, chat_type=chat_type, title=title,
                          username=None, registered_by=actor, default_timezone=timezone,
                          ui_language=locale)
        await connection.execute("UPDATE telegram_chats SET platform='max' WHERE telegram_chat_id=$1", chat)
    return actor, chat


async def remember_message(connection: Any, bot_id: int, chat_id: int, external_id: str) -> int:
    """The exact MAX message is used for subsequent edits; no replacement sends."""
    if not isinstance(external_id, str) or not 1 <= len(external_id) <= 256:
        raise ValueError('Invalid MAX message ID')
    return await connection.fetchval('''INSERT INTO max_messages(bot_id,chat_id,external_id)
        SELECT $1,$2,$3 FROM telegram_chats WHERE telegram_chat_id=$2 AND platform='max'
        ON CONFLICT(bot_id,chat_id,external_id) DO UPDATE SET external_id=EXCLUDED.external_id
        RETURNING id''', bot_id, chat_id, external_id)


async def message_external(connection: Any, bot_id: int, chat_id: int, identifier: int) -> str:
    result = await connection.fetchval('''SELECT external_id FROM max_messages
        WHERE id=$1 AND bot_id=$2 AND chat_id=$3''', identifier, bot_id, chat_id)
    if result is None:
        raise UserError('forbidden')
    return result
