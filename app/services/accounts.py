"""Telegram user and chat persistence."""

from __future__ import annotations

import asyncpg


async def upsert_user(
    connection: asyncpg.Connection,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    language_code: str | None,
    default_timezone: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_users(
            telegram_user_id, username, first_name, last_name,
            telegram_language_code, timezone
        ) VALUES($1, $2, $3, $4, $5, $6)
        ON CONFLICT (telegram_user_id) DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            last_name=EXCLUDED.last_name,
            telegram_language_code=EXCLUDED.telegram_language_code,
            updated_at=now()
        """,
        user_id,
        username,
        first_name,
        last_name,
        language_code,
        default_timezone,
    )


async def upsert_chat(
    connection: asyncpg.Connection,
    *,
    chat_id: int,
    chat_type: str,
    title: str | None,
    username: str | None,
    registered_by: int | None,
    default_timezone: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_chats(
            telegram_chat_id, chat_type, title, username,
            registered_by, timezone
        ) VALUES($1, $2, $3, $4, $5, $6)
        ON CONFLICT (telegram_chat_id) DO UPDATE SET
            chat_type=EXCLUDED.chat_type,
            title=EXCLUDED.title,
            username=EXCLUDED.username,
            registered_by=COALESCE(telegram_chats.registered_by, EXCLUDED.registered_by),
            is_active=true,
            updated_at=now()
        """,
        chat_id,
        chat_type,
        title,
        username,
        registered_by,
        default_timezone,
    )


async def claim_owner(
    connection: asyncpg.Connection,
    *,
    telegram_user_id: int,
    supplied_code: str,
    expected_code: str,
) -> bool:
    if not expected_code or supplied_code != expected_code:
        return False
    existing_owner = await connection.fetchval(
        "SELECT telegram_user_id FROM telegram_users WHERE is_owner=true LIMIT 1"
    )
    if existing_owner and int(existing_owner) != telegram_user_id:
        return False
    await connection.execute(
        "UPDATE telegram_users SET is_owner=true, updated_at=now() WHERE telegram_user_id=$1",
        telegram_user_id,
    )
    await connection.execute(
        """
        INSERT INTO app_settings(key, value) VALUES('owner_claimed', $1)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
        """,
        str(telegram_user_id),
    )
    return True


async def is_owner(connection: asyncpg.Connection, telegram_user_id: int) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT is_owner FROM telegram_users WHERE telegram_user_id=$1",
            telegram_user_id,
        )
    )
