"""aiogram adapter. Ambiguous API outcomes are never silently retried."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import BufferedInputFile

from app.services.errors import SendError
from app.services.formatting import plain_text, utf16_length
from app.services.rate_limit import wait_send_slot

LOGGER = logging.getLogger(__name__)


class TelegramSender:
    """Send through the shared limiter without logging bot tokens or payload text."""

    def __init__(self, bot: Any, connection: Any, settings: Any) -> None:
        self.bot, self.connection, self.settings = bot, connection, settings

    async def send(
        self, chat_id: int, text: Any, thread_id: int | None = None, *, reply_markup: Any = None
    ) -> int:
        """Map definitive Telegram rejections separately from uncertain transport failures."""
        await wait_send_slot(
            self.connection,
            chat_id,
            self.settings.telegram_global_rate_per_second,
            self.settings.telegram_chat_rate_per_second,
        )
        try:
            if isinstance(text, dict):
                message = await self._photo(chat_id, text, thread_id, reply_markup)
            else:
                message = await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                    reply_markup=reply_markup,
                    link_preview_options={"is_disabled": True},
                )
            return message.message_id
        except TelegramRetryAfter as error:
            raise SendError("retry", error.retry_after) from None
        except TelegramForbiddenError:
            raise SendError("forbidden") from None
        except TelegramBadRequest:
            raise SendError("rejected") from None
        except (TelegramNetworkError, TelegramServerError, TelegramAPIError, TimeoutError, OSError):
            raise SendError("uncertain") from None

    async def _photo(self, chat_id: int, chunk: dict, thread_id: int | None, markup: Any) -> Any:
        identifier, caption = chunk.get("image_id"), chunk.get("caption")
        if (
            chunk.get("kind") != "photo"
            or type(identifier) is not int
            or identifier <= 0
            or not isinstance(caption, str)
            or utf16_length(plain_text(caption)) > 1024
        ):
            raise SendError("rejected")
        image = await self.connection.fetchrow(
            """SELECT mime_type,telegram_file_id,telegram_bot_id,
            CASE WHEN telegram_bot_id=$2 AND telegram_file_id IS NOT NULL THEN NULL ELSE image_data END AS image_data
            FROM verse_illustrations WHERE id=$1 AND status='ready' """,
            identifier,
            self.bot.id,
        )
        if not image:
            raise SendError("rejected")
        cached = image["telegram_file_id"] if image["telegram_bot_id"] == self.bot.id else None
        suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[image["mime_type"]]
        photo = cached or BufferedInputFile(
            bytes(image["image_data"]), filename=f"verse-{identifier}.{suffix}"
        )
        kwargs = dict(
            chat_id=chat_id,
            caption=caption or None,
            parse_mode="HTML",
            message_thread_id=thread_id,
            reply_markup=markup,
            request_timeout=20,
        )
        try:
            sent = await self.bot.send_photo(photo=photo, **kwargs)
        except TelegramBadRequest:
            if not cached:
                raise
            data = await self.connection.fetchval(
                "SELECT image_data FROM verse_illustrations WHERE id=$1", identifier
            )
            sent = await self.bot.send_photo(
                photo=BufferedInputFile(bytes(data), filename=f"verse-{identifier}.{suffix}"),
                **kwargs,
            )
        if sent.photo:
            try:
                await self.connection.execute(
                    """UPDATE verse_illustrations SET telegram_file_id=$2,
                    telegram_bot_id=$3,updated_at=now() WHERE id=$1""",
                    identifier,
                    sent.photo[-1].file_id,
                    self.bot.id,
                )
            except Exception as error:
                LOGGER.warning("Illustration file cache deferred (%s)", type(error).__name__)
        try:
            from app.services.illustrations import record_view

            await record_view(self.connection, chat_id, identifier)
        except Exception as error:
            LOGGER.warning("Illustration view history deferred (%s)", type(error).__name__)
        return sent
