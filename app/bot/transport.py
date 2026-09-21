"""aiogram adapter. Ambiguous API outcomes are never silently retried."""
from __future__ import annotations
from typing import Any
from aiogram.exceptions import (TelegramRetryAfter, TelegramForbiddenError,
    TelegramBadRequest,TelegramNetworkError,TelegramServerError,TelegramAPIError)
from app.services.errors import SendError
from app.services.rate_limit import wait_send_slot


class TelegramSender:
    """Send through the shared limiter without logging bot tokens or payload text."""
    def __init__(self, bot: Any, connection: Any, settings: Any) -> None:
        self.bot,self.connection,self.settings = bot,connection,settings

    async def send(self, chat_id: int, text: str, thread_id: int | None = None,
                   *, reply_markup: Any = None) -> int:
        """Map definitive Telegram rejections separately from uncertain transport failures."""
        await wait_send_slot(self.connection,chat_id,self.settings.telegram_global_rate_per_second,
                             self.settings.telegram_chat_rate_per_second)
        try:
            message = await self.bot.send_message(chat_id,text,parse_mode='HTML',
                message_thread_id=thread_id,reply_markup=reply_markup,
                link_preview_options={'is_disabled':True})
            return message.message_id
        except TelegramRetryAfter as error:
            raise SendError('retry',error.retry_after) from None
        except TelegramForbiddenError:
            raise SendError('forbidden') from None
        except TelegramBadRequest:
            raise SendError('rejected') from None
        except (TelegramNetworkError,TelegramServerError,TelegramAPIError,TimeoutError,OSError):
            raise SendError('uncertain') from None
