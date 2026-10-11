"""Выбор транспорта по постоянному назначению чата, а не по отправителю входящего сообщения."""
from __future__ import annotations

import asyncio
import logging

from app.bot.transport import TelegramSender
from app.maxbot.client import MaxAPIError, MaxClient
from app.maxbot.config import MaxSettings
from app.maxbot.transport import MaxSender
from app.services.errors import SendError
from app.vkbot.client import VkClient
from app.vkbot.config import VkSettings
from app.vkbot.transport import VkSender


class PlatformTransports:
    """Набор транспортов (Telegram и MAX): MAX подключается лениво, чтобы доставка в Telegram не зависела от доступности MAX."""

    def __init__(self, telegram_bot, settings, *, max_client=None):
        """Запомнить бота Telegram и настройки, прочитать настройки MAX (при неверной конфигурации MAX отключается, Telegram продолжает работать)."""
        self.telegram_bot, self.settings = telegram_bot, settings
        try:
            config = MaxSettings.from_env()
        except ValueError:
            # A bad optional MAX configuration must not stop Telegram delivery.
            logging.getLogger(__name__).error('MAX configuration invalid; MAX transport unavailable')
            config = MaxSettings()
        self.max_client = max_client or (MaxClient(config) if config.token else None)
        self.max_bot_id = None
        try:
            vk = VkSettings.from_env()
        except ValueError:
            logging.getLogger(__name__).error('VK configuration invalid; VK transport unavailable')
            vk = VkSettings()
        self.vk_client = VkClient(vk) if vk.enabled else None
        self.vk_group_id = vk.group_id
        self._lock = asyncio.Lock()

    async def close(self):
        """Закрыть клиент MAX."""
        if self.max_client:
            await self.max_client.close()
        if self.vk_client:
            await self.vk_client.close()

    def sender(self, connection):
        """Создать отправителя, выбирающего транспорт по платформе чата."""
        return DestinationSender(self, connection)

    async def vk_sender(self, connection):
        """Получить отправителя ВКонтакте; недоступность VK — временный отказ (`retry`), Telegram продолжает работать."""
        if not self.vk_client:
            raise SendError('retry', 60)
        return VkSender(self.vk_client, self.vk_group_id, connection)

    async def max_sender(self, connection):
        """Получить отправителя MAX; при первом вызове проверяет, что токен принадлежит боту. Недоступность MAX — временный отказ (`retry`)."""
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
    """Отправитель, определяющий платформу чата по БД."""
    def __init__(self, transports, connection):
        """Запомнить набор транспортов и соединение."""
        self.transports, self.connection = transports, connection

    async def send(self, chat_id, text, thread_id=None, *, reply_markup=None):
        """Узнать платформу чата и отправить через Telegram или MAX; неизвестная платформа — отказ."""
        platform = await self.connection.fetchval('SELECT platform FROM telegram_chats WHERE telegram_chat_id=$1', chat_id)
        if platform == 'telegram':
            sender = TelegramSender(self.transports.telegram_bot, self.connection, self.transports.settings)
        elif platform == 'max':
            sender = await self.transports.max_sender(self.connection)
        elif platform == 'vk':
            sender = await self.transports.vk_sender(self.connection)
        else:
            raise SendError('rejected')
        return await sender.send(chat_id, text, thread_id, reply_markup=reply_markup)
