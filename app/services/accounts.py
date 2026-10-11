"""Хранение пользователей и чатов; входящие сообщения групп не переопределяют выбор назначения."""
from __future__ import annotations
import secrets
from typing import Any
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


async def is_owner(connection: Any, telegram_user_id: int) -> bool:
    """Проверить, что пользователь — владелец и не заблокирован."""
    return bool(await connection.fetchval('SELECT is_owner AND NOT is_blocked FROM telegram_users WHERE telegram_user_id=$1',telegram_user_id))
