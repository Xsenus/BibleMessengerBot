"""aiogram adapter. Ambiguous API outcomes are never silently retried."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaAudio,
    InputRichMessage,
    InputRichMessageMedia,
)

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
                if text.get("kind") in {"rich", "rich_edit"}:
                    message = await self._rich(chat_id, text, thread_id, reply_markup)
                else:
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
            if isinstance(text, dict) and text.get("kind") == "rich_edit":
                # Editing a frozen target is idempotent: a lost ACK cannot create
                # another message or consume another generation request.
                raise SendError("retry", 3) from None
            raise SendError("uncertain") from None

    async def _rich(self, chat_id, chunk, thread_id, markup):
        if chunk.get('card_id') is not None:
            from app.services.message_languages import outgoing
            chunk,markup = await outgoing(self.connection,chat_id,chunk)
        text, identifier = chunk.get("text"), chunk.get("image_id")
        editing = chunk["kind"] == "rich_edit"
        target = chunk.get("message_id")
        if (
            not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > 24000
            or (editing and (type(target) is not int or target <= 0))
            or (identifier is not None and (type(identifier) is not int or identifier <= 0))
        ):
            raise SendError("rejected")
        # Reply keyboards make Telegram messages non-editable. The persistent
        # navigation keyboard established by /start remains available.
        if markup is not None and not hasattr(markup, "inline_keyboard"):
            raise SendError("rejected")
        html = text.replace("\n", "<br>")
        audio = None
        if chunk.get('audio_id'):
            audio = await self.connection.fetchrow("SELECT * FROM reading_audio WHERE id=$1 AND state='ready'",chunk['audio_id'])
        audio_cached = audio and audio['telegram_bot_id']==self.bot.id and audio['telegram_file_id']
        async def remember_audio():
            if chunk.get('card_id') is not None:
                from app.services.speech import acknowledged
                await acknowledged(self.connection,chunk['card_id'],audio['id'] if audio else None)
        image = cached = None
        if identifier is not None:
            image = await self.connection.fetchrow(
                "SELECT mime_type,telegram_file_id,telegram_bot_id,image_data FROM verse_illustrations WHERE id=$1 AND status='ready'",
                identifier,
            )
            if not image:
                raise SendError("rejected")
            cached = image["telegram_file_id"] if image["telegram_bot_id"] == self.bot.id else None

        async def perform(use_cache):
            media = []
            if image is not None:
                suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[
                    image["mime_type"]
                ]
                photo = (
                    cached
                    if use_cache and cached
                    else BufferedInputFile(
                        bytes(image["image_data"]), filename=f"reading-{identifier}.{suffix}"
                    )
                )
                media = [InputRichMessageMedia(id="artwork", media=InputMediaPhoto(media=photo))]
            if audio is not None:
                recording = audio['telegram_file_id'] if use_cache and audio['telegram_bot_id']==self.bot.id else None
                recording = recording or BufferedInputFile(bytes(audio['audio_data']),filename=f"reading-{audio['language_code']}.mp3")
                media.append(InputRichMessageMedia(id='narration',media=InputMediaAudio(media=recording,
                    duration=audio['duration'],title='Библейское чтение · '+audio['language_code'],performer='Синтетическая озвучка')))
            rich = InputRichMessage(
                html=('<img src="tg://photo?id=artwork"/>' if image else "") + html + ('<audio src="tg://audio?id=narration"></audio>' if audio else ''),
                media=media or None,
            )
            if editing:
                return await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=target,
                    rich_message=rich,
                    parse_mode=None,
                    reply_markup=markup,
                    request_timeout=20,
                )
            return await self.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich,
                message_thread_id=thread_id,
                reply_markup=markup,
                request_timeout=20,
            )

        try:
            try:
                sent = await perform(bool(cached or audio_cached))
            except TelegramBadRequest as error:
                if editing and "message is not modified" in error.message.lower():
                    await remember_audio()
                    return SimpleNamespace(message_id=target)
                # Retry only a definite stale-file rejection. Deleted/non-editable
                # messages must never turn into replacement sends.
                if not (cached or audio_cached) or not any(
                    s in error.message.lower()
                    for s in ("file identifier", "file_id", "wrong remote file")
                ):
                    raise
                sent = await perform(False)
        except TelegramBadRequest as error:
            if editing and "message is not modified" in error.message.lower():
                await remember_audio()
                return SimpleNamespace(message_id=target)
            raise
        if image is not None:
            try:
                for block in sent.rich_message.blocks:
                    if getattr(block, "type", None) == "photo" and block.photo:
                        await self.connection.execute(
                            "UPDATE verse_illustrations SET telegram_file_id=$2,telegram_bot_id=$3,updated_at=now() WHERE id=$1",
                            identifier,
                            block.photo[-1].file_id,
                            self.bot.id,
                        )
                        break
                from app.services.illustrations import record_view

                await record_view(self.connection, chat_id, identifier)
            except Exception as error:
                LOGGER.warning("Reading artwork cache deferred (%s)", type(error).__name__)
        if chunk.get('card_id') is not None and not editing:
            from app.services.message_languages import bind
            await bind(self.connection,chunk['card_id'],chat_id,sent.message_id)
        await remember_audio()
        if audio:
            try:
                for block in sent.rich_message.blocks:
                    if getattr(block,'type',None)=='audio' and block.audio:
                        await self.connection.execute('UPDATE reading_audio SET telegram_file_id=$2,telegram_bot_id=$3 WHERE id=$1',
                            audio['id'],block.audio.file_id,self.bot.id)
                        break
            except Exception as error:
                LOGGER.warning('Speech cache deferred (%s)',type(error).__name__)
        return sent

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
