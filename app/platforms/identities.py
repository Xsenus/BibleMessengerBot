"""Постоянное соответствие внешних идентификаторов платформы внутренним ключам БД.

Числа разных платформ не пересекаются с Telegram: внутренние ID выделяются из отдельной последовательности и проверяются на
занятость. Совпадение числовых ID на разных платформах не объединяет аккаунты."""
from __future__ import annotations

from typing import Any

from app.platforms.spec import PlatformSpec
from app.services.accounts import upsert_chat, upsert_user
from app.services.errors import UserError
from app.services.locks import lock_key


class IdentityStore:
    """Сопоставление пользователей и чатов одной платформы."""

    def __init__(self, spec: PlatformSpec) -> None:
        self.spec = spec

    def valid_external_id(self, value: object) -> int:
        """Проверить внешний ID (ненулевое целое в диапазоне int64) и вернуть его."""
        if type(value) is not int or not -(2**63) < value < 2**63 or value == 0:
            raise ValueError(f'Invalid {self.spec.title} identifier')
        return value

    async def lookup(self, connection: Any, kind: str, external_id: int) -> int | None:
        """Найти внутренний ID по внешнему (пользователь или чат); если соответствия нет — None."""
        return await connection.fetchval(
            'SELECT internal_id FROM platform_identities WHERE platform=$1 AND kind=$2 AND external_id=$3',
            self.spec.name, kind, self.valid_external_id(external_id))

    async def external(self, connection: Any, kind: str, internal_id: int) -> int:
        """Найти внешний ID по внутреннему; неизвестный — отказ (`forbidden`)."""
        value = await connection.fetchval(
            'SELECT external_id FROM platform_identities WHERE platform=$1 AND kind=$2 AND internal_id=$3',
            self.spec.name, kind, internal_id)
        if value is None:
            raise UserError('forbidden')
        return value

    async def _allocate(self, connection: Any, kind: str, external_id: int, *, shared_id: int | None = None) -> int:
        """Выделить (или вернуть уже выделенный) внутренний ID из отдельной последовательности, проверяя, что число не занято
        ни пользователем, ни чатом, ни другим соответствием. Вызывается под общей блокировкой платформы."""
        if kind not in {'user', 'chat'}:
            raise ValueError('Invalid identity kind')
        self.valid_external_id(external_id)
        previous = await self.lookup(connection, kind, external_id)
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
        await connection.execute('INSERT INTO platform_identities(platform,kind,external_id,internal_id) VALUES($1,$2,$3,$4)',
                                 self.spec.name, kind, external_id, identifier)
        return identifier

    async def register(self, connection: Any, *, user: dict, external_chat_id: int, chat_type: str,
                       timezone: str, locale: str = 'ru', title: str | None = None) -> tuple[int, int]:
        """Атомарно сопоставить пользователя и чат из проверенного события и создать их настройки. У личного чата внутренний ID
        совпадает с ID пользователя (так работает общая проверка прав). Возвращает (пользователь, чат)."""
        if chat_type not in {'private', 'group', 'channel'}:
            raise ValueError(f'Invalid {self.spec.title} chat type')
        name = self.spec.name
        async with connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key(f'{name}-identities', 1))
            actor = await self._allocate(connection, 'user', self.valid_external_id(user.get('user_id')))
            chat = await self._allocate(connection, 'chat', external_chat_id, shared_id=actor if chat_type == 'private' else None)
            await upsert_user(connection, user_id=actor, username=user.get('username'), first_name=user.get('first_name'),
                              last_name=user.get('last_name'), language_code=locale, default_timezone=timezone)
            await connection.execute('UPDATE telegram_users SET platform=$2 WHERE telegram_user_id=$1', actor, name)
            await upsert_chat(connection, chat_id=chat, chat_type=chat_type, title=title, username=None, registered_by=actor,
                              default_timezone=timezone, ui_language=locale)
            await connection.execute('UPDATE telegram_chats SET platform=$2 WHERE telegram_chat_id=$1', chat, name)
        return actor, chat
