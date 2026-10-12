"""Постоянное соответствие идентификаторов MAX внутренним ключам БД; совпадение чисел с Telegram не объединяет аккаунты (общая реализация — app.platforms.identities)."""
from __future__ import annotations

from typing import Any

from app.platforms.builtin import MAX
from app.platforms.identities import IdentityStore
from app.services.errors import UserError

store = IdentityStore(MAX)
valid_external_id = store.valid_external_id
lookup = store.lookup
external = store.external
register = store.register


async def remember_message(connection: Any, bot_id: int, chat_id: int, external_id: str) -> int:
    """Запомнить сообщение MAX и вернуть его внутренний числовой ID; именно это сообщение потом правится (повторных отправок нет)."""
    if not isinstance(external_id, str) or not 1 <= len(external_id) <= 256:
        raise ValueError('Invalid MAX message ID')
    return await connection.fetchval('''INSERT INTO max_messages(bot_id,chat_id,external_id)
        SELECT $1,$2,$3 FROM telegram_chats WHERE telegram_chat_id=$2 AND platform='max'
        ON CONFLICT(bot_id,chat_id,external_id) DO UPDATE SET external_id=EXCLUDED.external_id
        RETURNING id''', bot_id, chat_id, external_id)


async def message_external(connection: Any, bot_id: int, chat_id: int, identifier: int) -> str:
    """Вернуть внешний ID сообщения MAX по внутреннему для данного бота и чата; чужое — отказ."""
    result = await connection.fetchval('''SELECT external_id FROM max_messages
        WHERE id=$1 AND bot_id=$2 AND chat_id=$3''', identifier, bot_id, chat_id)
    if result is None:
        raise UserError('forbidden')
    return result
