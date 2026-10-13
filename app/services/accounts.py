"""Хранение пользователей и чатов; входящие сообщения групп не переопределяют выбор назначения."""
from __future__ import annotations
import hashlib
import secrets
from typing import Any
from app.services.errors import UserError
from app.services.i18n import initial_ui
from app.services.locks import lock_key


async def upsert_user(connection: Any, *, user_id: int, username: str | None,
                      first_name: str | None, last_name: str | None,
                      language_code: str | None, default_timezone: str) -> None:
    """Создать или обновить пользователя Telegram (имя, язык и т. п.), сохраняя признаки владельца, блокировки и настройки."""
    await connection.execute('''INSERT INTO telegram_users(telegram_user_id,username,first_name,last_name,
        telegram_language_code,timezone) VALUES($1,$2,$3,$4,$5,$6)
        ON CONFLICT(telegram_user_id) DO UPDATE SET username=EXCLUDED.username,
        first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,
        telegram_language_code=EXCLUDED.telegram_language_code,updated_at=now()''',
        user_id,username,first_name,last_name,language_code,default_timezone)


async def upsert_chat(connection: Any, *, chat_id: int, chat_type: str, title: str | None,
                      username: str | None, registered_by: int | None,
                      default_timezone: str, ui_language: str = 'ru') -> None:
    """Создать или обновить чат: язык интерфейса задаётся один раз при создании, настройки администратора в канал не копируются; личный чат всегда активен."""
    await connection.execute('''INSERT INTO telegram_chats(telegram_chat_id,chat_type,title,username,
        registered_by,timezone,ui_language) VALUES($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT(telegram_chat_id) DO UPDATE SET chat_type=EXCLUDED.chat_type,
        title=EXCLUDED.title,username=EXCLUDED.username,
        registered_by=COALESCE(telegram_chats.registered_by,EXCLUDED.registered_by),
        is_active=CASE WHEN EXCLUDED.chat_type='private' THEN true ELSE telegram_chats.is_active END,
        updated_at=now()''',chat_id,chat_type,title,username,registered_by,default_timezone,initial_ui(ui_language))


async def claim_owner(connection: Any, *, telegram_user_id: int, supplied_code: str,
                      expected_code: str) -> bool:
    """Назначить владельца бота по секретному коду (сравнение за постоянное время, один владелец и один раз, под блокировкой). Возвращает True при успехе."""
    if len(expected_code) < 16 or not secrets.compare_digest(supplied_code.encode('utf-8'),expected_code.encode('utf-8')):
        return False
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)',lock_key('owner',1))
        if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM app_settings WHERE key='owner_claimed')"):
            return False
        if await connection.fetchval('SELECT EXISTS(SELECT 1 FROM telegram_users WHERE is_owner=true)'):
            return False
        result = await connection.execute('UPDATE telegram_users SET is_owner=true,updated_at=now() WHERE telegram_user_id=$1 AND NOT is_blocked',telegram_user_id)
        if result != 'UPDATE 1':
            return False
        await connection.execute("INSERT INTO app_settings(key,value) VALUES('owner_claimed',$1)",str(telegram_user_id))
    return True


async def is_admin(connection: Any, telegram_user_id: int) -> bool:
    """Администратор бота (владелец или назначенный администратор) и не заблокирован."""
    return bool(await connection.fetchval(
        'SELECT (is_owner OR is_admin) AND NOT is_blocked FROM telegram_users WHERE telegram_user_id=$1', telegram_user_id))


ROLES = ('viewer', 'editor', 'admin', 'owner')       # по возрастанию прав


async def role_of(connection: Any, telegram_user_id: int) -> str | None:
    """Роль пользователя: owner, admin, editor, viewer; None — не администратор или заблокирован."""
    row = await connection.fetchrow(
        'SELECT is_owner,is_admin,admin_role,is_blocked FROM telegram_users WHERE telegram_user_id=$1', telegram_user_id)
    if not row or row['is_blocked']:
        return None
    if row['is_owner']:
        return 'owner'
    return row['admin_role'] if row['is_admin'] else None


async def require_role(connection: Any, telegram_user_id: int, minimum: str = 'editor') -> str:
    """Проверить, что у пользователя роль не ниже `minimum` (просмотр — viewer, изменения — editor, настройки — admin)."""
    role = await role_of(connection, telegram_user_id)
    if role is None or ROLES.index(role) < ROLES.index(minimum):
        raise UserError('forbidden')
    return role


def _invite_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode('utf-8')).hexdigest()


async def create_admin_invite(connection: Any, owner_id: int, hours: int = 24, role: str = 'admin') -> str:
    """Выдать одноразовый код приглашения администратора (только владелец). Код действует `hours` часов."""
    if not await is_owner(connection, owner_id):
        raise UserError('forbidden')
    if role not in {'viewer', 'editor', 'admin'}:
        raise UserError('invalid')
    code = secrets.token_urlsafe(9)
    await connection.execute(
        "INSERT INTO admin_invites(code_hash,created_by,expires_at,role) VALUES($1,$2,now()+make_interval(hours=>$3),$4)",
        _invite_hash(code), owner_id, hours, role)
    return code


async def redeem_admin_invite(connection: Any, telegram_user_id: int, code: str) -> bool:
    """Принять приглашение: пользователь (с любой платформы) становится администратором. Код сгорает."""
    async with connection.transaction():
        invite = await connection.fetchrow(
            """UPDATE admin_invites SET used_by=$2,used_at=now() WHERE code_hash=$1 AND used_at IS NULL
               AND expires_at>now() RETURNING id,role""", _invite_hash(code), telegram_user_id)
        if not invite:
            return False
        result = await connection.execute(
            'UPDATE telegram_users SET is_admin=true,admin_role=$2,updated_at=now() WHERE telegram_user_id=$1 AND NOT is_blocked',
            telegram_user_id, invite['role'])
        if result != 'UPDATE 1':
            raise UserError('forbidden')
    return True


async def list_admins(connection: Any) -> list[Any]:
    """Владелец и администраторы с именем и платформой."""
    return await connection.fetch(
        """SELECT telegram_user_id,platform,username,first_name,is_owner,admin_role FROM telegram_users
           WHERE (is_owner OR is_admin) AND NOT is_blocked ORDER BY is_owner DESC,telegram_user_id""")


async def remove_admin(connection: Any, owner_id: int, telegram_user_id: int) -> bool:
    """Снять права администратора (только владелец; владельца снять нельзя)."""
    if not await is_owner(connection, owner_id):
        raise UserError('forbidden')
    result = await connection.execute(
        'UPDATE telegram_users SET is_admin=false,updated_at=now() WHERE telegram_user_id=$1 AND is_admin AND NOT is_owner', telegram_user_id)
    return result == 'UPDATE 1'


async def is_owner(connection: Any, telegram_user_id: int) -> bool:
    """Проверить, что пользователь — владелец и не заблокирован."""
    return bool(await connection.fetchval('SELECT is_owner AND NOT is_blocked FROM telegram_users WHERE telegram_user_id=$1',telegram_user_id))
