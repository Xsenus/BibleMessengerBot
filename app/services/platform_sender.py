"""Select transport from the persistent destination, never from the incoming actor."""
from __future__ import annotations

import asyncio
import logging

from app.bot.transport import TelegramSender
from app.maxbot.client import MaxAPIError, MaxClient
from app.maxbot.config import MaxSettings
from app.maxbot.transport import MaxSender
from app.services.errors import SendError


class PlatformTransports:
    """Lazy MAX discovery keeps Telegram delivery independent of MAX availability."""

    def __init__(self, telegram_bot, settings, *, max_client=None):
        self.telegram_bot, self.settings = telegram_bot, settings
        try:
            config = MaxSettings.from_env()
        except ValueError:
            # A bad optional MAX configuration must not stop Telegram delivery.
            logging.getLogger(__name__).error('MAX configuration invalid; MAX transport unavailable')
            config = MaxSettings()
        self.max_client = max_client or (MaxClient(config) if config.token else None)
        self.max_bot_id = None
        self._lock = asyncio.Lock()

    async def close(self):
        if self.max_client:
            await self.max_client.close()

    def sender(self, connection):
        return DestinationSender(self, connection)

    async def max_sender(self, connection):
        if not self.max_client:
            raise SendError('retry', 60)
        async with self._lock:
            if self.max_bot_id is None:
                try:
                    info = await self.max_client.get_me()
                except MaxAPIError:
                    raise SendError('retry', 60) from None
                if type(info.get('user_id')) is not int or info['user_id']<=0 or not info.get('is_bot'):
                    raise SendError('retry', 60)
                self.max_bot_id = info['user_id']
        return MaxSender(self.max_client, self.max_bot_id, connection)


class DestinationSender:
    def __init__(self, transports, connection):
        self.transports, self.connection = transports, connection

    async def send(self, chat_id, text, thread_id=None, *, reply_markup=None):
        platform = await self.connection.fetchval('SELECT platform FROM telegram_chats WHERE telegram_chat_id=$1', chat_id)
        if platform == 'telegram':
            sender = TelegramSender(self.transports.telegram_bot, self.connection, self.transports.settings)
        elif platform == 'max':
            sender = await self.transports.max_sender(self.connection)
        else:
            raise SendError('rejected')
        return await sender.send(chat_id, text, thread_id, reply_markup=reply_markup)
