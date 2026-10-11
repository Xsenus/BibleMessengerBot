"""Постоянное соответствие идентификаторов ВКонтакте внутренним ключам БД; совпадение чисел с Telegram не объединяет аккаунты."""
from __future__ import annotations

from typing import Any

from app.services.accounts import upsert_chat, upsert_user
from app.services.errors import UserError
from app.services.locks import lock_key


def valid_external_id(value: object) -> int:
    """Проверить внешний ID VK (ненулевое целое в диапазоне int64) и вернуть его."""
    if type(value) is not int or not -(2**63) < value < 2**63 or value == 0:
        raise ValueError('Invalid VK identifier')
    return value


async def lookup(connection: Any, kind: str, external_id: int) -> int | None:
    """Найти внутренний ID по внешнему ID VK (пользователь или чат); если соответствия нет — None."""
    return await connection.fetchval(
        "SELECT internal_id FROM platform_identities WHERE platform='vk' AND kind=$1 AND external_id=$2",
        kind, valid_external_id(external_id),
    )


async def external(connection: Any, kind: str, internal_id: int) -> int:
    """Найти внешний ID VK по внутреннему; неизвестный — отказ (`forbidden`)."""
    value = await connection.fetchval(
        "SELECT external_id FROM platform_identities WHERE platform='vk' AND kind=$1 AND internal_id=$2",
        kind, internal_id,
    )
    if value is None:
        raise UserError('forbidden')
    return value


async def _allocate(connection: Any, kind: str, external_id: int,
                    *, shared_id: int | None = None) -> int:
    """Выделить (или вернуть уже выделенный) внутренний ID для внешнего ID VK из отдельной последовательности, проверяя, что число не занято ни пользователем, ни чатом, ни другим соответствием. Вызывается под общей блокировкой."""
    if kind not in {'user', 'chat'}:
        raise ValueError('Invalid identity kind')
    valid_external_id(external_id)
    # Called under the global VK mapping lock. Check actual existing identities,
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
        VALUES('vk',$1,$2,$3)''', kind, external_id, identifier)
    return identifier


async def register(connection: Any, *, user: dict, external_chat_id: int, chat_type: str,
                   timezone: str, locale: str = 'ru', title: str | None = None) -> tuple[int, int]:
    """Атомарно сопоставить пользователя и чат из проверенного события VK и создать их настройки. У личного чата внутренний ID совпадает с ID пользователя (так работает общая проверка прав). Возвращает (пользователь, чат)."""
    if chat_type not in {'private', 'group', 'channel'}:
        raise ValueError('Invalid VK chat type')
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key('vk-identities', 1))
        actor = await _allocate(connection, 'user', valid_external_id(user.get('user_id')))
        chat = await _allocate(connection, 'chat', external_chat_id,
                               shared_id=actor if chat_type == 'private' else None)
        await upsert_user(connection, user_id=actor, username=user.get('username'),
                          first_name=user.get('first_name'), last_name=user.get('last_name'),
                          language_code=locale, default_timezone=timezone)
        await connection.execute("UPDATE telegram_users SET platform='vk' WHERE telegram_user_id=$1", actor)
        await upsert_chat(connection, chat_id=chat, chat_type=chat_type, title=title,
                          username=None, registered_by=actor, default_timezone=timezone,
                          ui_language=locale)
        await connection.execute("UPDATE telegram_chats SET platform='vk' WHERE telegram_chat_id=$1", chat)
    return actor, chat
