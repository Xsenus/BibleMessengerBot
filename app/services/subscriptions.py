"""Subscription creation, updates, and status helpers."""

from __future__ import annotations

from datetime import time

import asyncpg

from app.services.scheduling import next_occurrence, validate_timezone

VALID_MODES = {"sequential", "verse_of_day", "topic_of_day", "reading_plan"}


async def create_or_update_subscription(
    connection: asyncpg.Connection,
    *,
    chat_id: int,
    created_by: int | None,
    translation_id: int,
    mode: str,
    send_time: time,
    timezone_name: str,
    plan_code: str | None = None,
) -> asyncpg.Record:
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    validate_timezone(timezone_name)
    next_run = next_occurrence(send_time, timezone_name)
    return await connection.fetchrow(
        """
        INSERT INTO subscriptions(
            telegram_chat_id, created_by, translation_id, mode,
            send_time, timezone, plan_code, next_run_at, is_enabled
        ) VALUES($1, $2, $3, $4, $5, $6, $7, $8, true)
        ON CONFLICT (telegram_chat_id, mode) DO UPDATE SET
            created_by=EXCLUDED.created_by,
            translation_id=EXCLUDED.translation_id,
            send_time=EXCLUDED.send_time,
            timezone=EXCLUDED.timezone,
            plan_code=EXCLUDED.plan_code,
            next_run_at=EXCLUDED.next_run_at,
            is_enabled=true,
            updated_at=now()
        RETURNING *
        """,
        chat_id,
        created_by,
        translation_id,
        mode,
        send_time,
        timezone_name,
        plan_code,
        next_run,
    )


async def set_enabled(
    connection: asyncpg.Connection,
    chat_id: int,
    enabled: bool,
    mode: str | None = None,
) -> int:
    query = """
        UPDATE subscriptions
        SET is_enabled=$2,
            next_run_at=CASE WHEN $2 THEN next_run_at ELSE NULL END,
            updated_at=now()
        WHERE telegram_chat_id=$1
    """
    args: list[object] = [chat_id, enabled]
    if mode:
        query += " AND mode=$3"
        args.append(mode)
    result = await connection.execute(query, *args)
    return int(result.split()[-1])


async def delete_subscriptions(
    connection: asyncpg.Connection,
    chat_id: int,
    mode: str | None = None,
) -> int:
    if mode:
        result = await connection.execute(
            "DELETE FROM subscriptions WHERE telegram_chat_id=$1 AND mode=$2",
            chat_id,
            mode,
        )
    else:
        result = await connection.execute(
            "DELETE FROM subscriptions WHERE telegram_chat_id=$1",
            chat_id,
        )
    return int(result.split()[-1])


async def list_subscriptions(connection: asyncpg.Connection, chat_id: int) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        SELECT s.*, t.title AS translation_title, t.source_translation_id
        FROM subscriptions s JOIN translations t ON t.id=s.translation_id
        WHERE s.telegram_chat_id=$1
        ORDER BY s.mode
        """,
        chat_id,
    )
