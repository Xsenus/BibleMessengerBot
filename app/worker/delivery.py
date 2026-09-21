"""Build and send subscription payloads with idempotency and throttling."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.config import Settings
from app.services.bible import (
    next_chapter_reference,
    render_chapter,
    render_verse,
    topic_verse,
    verse_of_day,
)
from app.services.formatting import escape, split_message
from app.services.scheduling import next_occurrence


class RateLimiter:
    def __init__(self, rate_per_second: float) -> None:
        self.interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self.interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


@dataclass(slots=True)
class DeliveryPayload:
    key: str
    text: str
    next_book_code: str | None = None
    next_chapter: int | None = None
    completed: bool = False


async def _translation(connection: asyncpg.Connection, translation_id: int) -> asyncpg.Record:
    row = await connection.fetchrow(
        """
        SELECT t.*, l.code AS language_code
        FROM translations t JOIN languages l ON l.id=t.language_id
        WHERE t.id=$1 AND t.is_active=true
        """,
        translation_id,
    )
    if not row:
        raise ValueError(f"Translation {translation_id} is unavailable")
    return row


def _key(*values: object) -> str:
    return hashlib.sha256(":".join(str(value) for value in values).encode()).hexdigest()


async def build_payload(
    connection: asyncpg.Connection,
    subscription: asyncpg.Record,
    *,
    scheduled_for: datetime,
) -> DeliveryPayload:
    translation = await _translation(connection, subscription["translation_id"])
    mode = subscription["mode"]
    seed = str(subscription["telegram_chat_id"])
    local_date = scheduled_for.astimezone(ZoneInfo(subscription["timezone"])).date()

    if mode == "verse_of_day":
        row = await verse_of_day(connection, translation, seed, local_date)
        if not row:
            raise ValueError("No verse available")
        text = await render_verse(connection, row, translation)
        return DeliveryPayload(
            _key(subscription["id"], local_date, mode, row["book_code"], row["chapter"], row["verse"]),
            text,
        )

    if mode == "topic_of_day":
        result = await topic_verse(
            connection,
            translation,
            seed=seed,
            on_date=local_date,
        )
        if not result:
            raise ValueError("No topic verse available")
        title, row = result
        text = f"<b>Тема дня: {escape(title)}</b>\n\n" + await render_verse(
            connection, row, translation
        )
        return DeliveryPayload(
            _key(subscription["id"], local_date, mode, title, row["book_code"], row["chapter"], row["verse"]),
            text,
        )

    testament = "NT" if subscription["plan_code"] == "new-testament-90" else None
    reference = await next_chapter_reference(
        connection,
        translation["id"],
        subscription["current_book_code"],
        subscription["current_chapter"],
        testament=testament,
    )
    if reference is None:
        return DeliveryPayload(
            _key(subscription["id"], "complete"),
            "<b>План чтения завершён.</b>",
            completed=True,
        )
    book_code, chapter = reference
    rendered = await render_chapter(connection, translation, book_code, chapter)
    if not rendered:
        raise ValueError(f"Chapter {book_code} {chapter} is empty")
    return DeliveryPayload(
        _key(subscription["id"], mode, book_code, chapter),
        rendered,
        next_book_code=book_code,
        next_chapter=chapter,
    )


async def reserve_delivery(
    connection: asyncpg.Connection,
    subscription: asyncpg.Record,
    payload: DeliveryPayload,
    scheduled_for: datetime,
) -> int | None:
    delivery_id = await connection.fetchval(
        """
        INSERT INTO delivery_log(
            subscription_id, telegram_chat_id, translation_id, mode,
            scheduled_for, payload_key, payload_preview, status, attempt_count
        ) VALUES($1, $2, $3, $4, $5, $6, $7, 'sending', 1)
        ON CONFLICT (telegram_chat_id, payload_key) DO NOTHING
        RETURNING id
        """,
        subscription["id"],
        subscription["telegram_chat_id"],
        subscription["translation_id"],
        subscription["mode"],
        scheduled_for,
        payload.key,
        payload.text[:500],
    )
    if delivery_id is not None:
        return int(delivery_id)

    existing = await connection.fetchrow(
        """
        SELECT id, status, attempt_count FROM delivery_log
        WHERE telegram_chat_id=$1 AND payload_key=$2
        """,
        subscription["telegram_chat_id"],
        payload.key,
    )
    if not existing or existing["status"] in {"sent", "sending"}:
        return None
    await connection.execute(
        """
        UPDATE delivery_log SET status='sending', attempt_count=attempt_count+1,
            error_code=NULL, error_message=NULL, updated_at=now()
        WHERE id=$1
        """,
        existing["id"],
    )
    return int(existing["id"])


async def deliver_one(
    bot: Bot,
    connection: asyncpg.Connection,
    subscription: asyncpg.Record,
    settings: Settings,
    global_limiter: RateLimiter,
    chat_limiters: dict[int, RateLimiter],
) -> None:
    scheduled_for = subscription["next_run_at"] or datetime.now(timezone.utc)
    payload = await build_payload(connection, subscription, scheduled_for=scheduled_for)
    delivery_id = await reserve_delivery(connection, subscription, payload, scheduled_for)
    if delivery_id is None:
        await _finish_subscription(connection, subscription, payload, settings)
        return

    chat_id = int(subscription["telegram_chat_id"])
    chat_limiter = chat_limiters.setdefault(
        chat_id, RateLimiter(settings.telegram_chat_rate_per_second)
    )
    message_ids: list[int] = []
    try:
        for chunk in split_message(payload.text, settings.max_message_length):
            await global_limiter.wait()
            await chat_limiter.wait()
            message = await bot.send_message(chat_id, chunk)
            message_ids.append(message.message_id)
        await connection.execute(
            """
            UPDATE delivery_log SET status='sent', telegram_message_ids=$2,
                sent_at=now(), updated_at=now()
            WHERE id=$1
            """,
            delivery_id,
            message_ids,
        )
        await _finish_subscription(connection, subscription, payload, settings)
    except TelegramRetryAfter as exc:
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=int(exc.retry_after) + 2)
        await _mark_retry(connection, delivery_id, subscription["id"], "retry_after", str(exc), retry_at)
    except TelegramForbiddenError as exc:
        await connection.execute(
            """
            UPDATE delivery_log SET status='failed', error_code='forbidden',
                error_message=$2, updated_at=now() WHERE id=$1
            """,
            delivery_id,
            str(exc)[:4000],
        )
        await connection.execute(
            "UPDATE subscriptions SET is_enabled=false, locked_at=NULL, lock_token=NULL WHERE id=$1",
            subscription["id"],
        )
        await connection.execute(
            "UPDATE telegram_chats SET is_active=false, updated_at=now() WHERE telegram_chat_id=$1",
            chat_id,
        )
    except Exception as exc:  # noqa: BLE001 - worker must retry transient Telegram/network errors
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        await _mark_retry(
            connection,
            delivery_id,
            subscription["id"],
            type(exc).__name__,
            str(exc),
            retry_at,
        )


async def _finish_subscription(
    connection: asyncpg.Connection,
    subscription: asyncpg.Record,
    payload: DeliveryPayload,
    settings: Settings,
) -> None:
    next_run = None if payload.completed else next_occurrence(
        subscription["send_time"],
        subscription["timezone"],
        list(subscription["days_of_week"]),
        now=datetime.now(timezone.utc),
    )
    await connection.execute(
        """
        UPDATE subscriptions SET
            current_book_code=COALESCE($2, current_book_code),
            current_chapter=COALESCE($3, current_chapter),
            last_run_at=now(), next_run_at=$4,
            is_enabled=CASE WHEN $5 THEN false ELSE is_enabled END,
            locked_at=NULL, lock_token=NULL, updated_at=now()
        WHERE id=$1
        """,
        subscription["id"],
        payload.next_book_code,
        payload.next_chapter,
        next_run,
        payload.completed,
    )


async def _mark_retry(
    connection: asyncpg.Connection,
    delivery_id: int,
    subscription_id: int,
    code: str,
    message: str,
    retry_at: datetime,
) -> None:
    await connection.execute(
        """
        UPDATE delivery_log SET status='retry', error_code=$2, error_message=$3,
            updated_at=now() WHERE id=$1
        """,
        delivery_id,
        code[:100],
        message[:4000],
    )
    await connection.execute(
        """
        UPDATE subscriptions SET next_run_at=$2, locked_at=NULL, lock_token=NULL,
            updated_at=now() WHERE id=$1
        """,
        subscription_id,
        retry_at,
    )
