"""Мост ВКонтакте: права и модели VK за тем же интерфейсом, что использует общий обработчик команд.

DTO aiogram — лишь внутренняя форма команд и нажатий; API Telegram для VK не вызывается, вся публикация идёт
через общую очередь доставки и отправителя VK.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Chat

from app.maxbot.bridge import user_model  # noqa: F401  (общая модель пользователя aiogram)
from app.services import bible, illustrations, message_languages
from app.services.errors import UserError
from app.services.formatting import split_message
from app.vkbot.client import VkAPIError, VkClient
from app.vkbot.identities import external, lookup
from app.vkbot.transport import VkSender
from app.vkbot.ui import keyboard_pages, persistent
from app.worker.delivery import insert_payload

LOGGER = logging.getLogger(__name__)
CONVERSATION_BASE = 2_000_000_000


@dataclass(slots=True)
class ReplyContext:
    key: str
    position: int = 0


class VkBotBridge:
    """Объект, притворяющийся ботом aiogram для общих обработчиков: отвечает на `get_me`, `get_chat`,
    `get_chat_member`, подтверждает нажатия и ставит ответы в очередь доставки."""
    platform = 'vk'

    def __init__(self, client: VkClient, pool: Any, info: dict) -> None:
        self.client, self.pool = client, pool
        self.id = int(info['id'])                      # номер сообщества
        self.info = dict(info, username=info.get('screen_name'))
        self.reply_context: ContextVar[ReplyContext | None] = ContextVar('vk_reply_context', default=None)
        self.event_context: ContextVar[dict | None] = ContextVar('vk_event_context', default=None)

    def sender(self, connection: Any) -> VkSender:
        """Создать отправителя VK для указанного соединения."""
        return VkSender(self.client, self.id, connection)

    async def get_me(self) -> SimpleNamespace:
        """Данные самого бота (id и имя) в виде, понятном общим обработчикам."""
        return SimpleNamespace(id=self.id, username=self.info.get('screen_name'))

    async def get_chat(self, identifier: Any) -> Chat:
        """Найти чат VK по внутреннему идентификатору (чужие платформы не раскрываются)."""
        if type(identifier) is not int:
            raise UserError('no_result')
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='vk'", identifier)
            if not row:
                mapped = await lookup(connection, 'chat', identifier)
                row = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='vk'", mapped) if mapped is not None else None
        if not row:
            raise UserError('forbidden')
        return Chat(id=row['telegram_chat_id'], type='private' if row['chat_type'] == 'private' else 'group', title=row['title'])

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        """Роль участника беседы: создатель, администратор или участник; для бота — право писать в беседе.

        В личном диалоге права проверяет общий код (чат совпадает с пользователем), поэтому ответ тривиален.
        """
        async with self.pool.acquire() as connection:
            peer = await external(connection, 'chat', chat_id)
            actor = None if user_id == self.id else await external(connection, 'user', user_id)
        if peer < CONVERSATION_BASE:
            return SimpleNamespace(status='administrator' if user_id == self.id else 'creator', can_post_messages=True)
        try:
            members = await self.client.call('messages.getConversationMembers', peer_id=peer, group_id=self.id)
        except VkAPIError:
            raise UserError('forbidden') from None
        wanted = -self.id if actor is None else actor
        item = next((m for m in members.get('items', []) if m.get('member_id') == wanted), None)
        if item is None:
            raise UserError('forbidden')
        if actor is None:
            return SimpleNamespace(status='administrator', can_post_messages=True)
        status = 'creator' if item.get('is_owner') else 'administrator' if item.get('is_admin') else 'member'
        return SimpleNamespace(status=status, can_post_messages=True)

    async def __call__(self, method: Any, **_: Any) -> bool:
        """Выполнить вызов «ответ на нажатие кнопки» (единственный поддерживаемый метод): идемпотентно."""
        if not isinstance(method, AnswerCallbackQuery):
            raise UserError('invalid')
        event = self.event_context.get()
        if event:
            try:
                await self.client.answer_event(event['event_id'], event['user_id'], event['peer_id'], method.text)
            except VkAPIError as error:
                LOGGER.warning('VK callback acknowledgement deferred: %s', error.code)
        return True

    def _key(self) -> str:
        """Выдать следующий ключ идемпотентности ответа текущего события; вне контекста события — ошибка."""
        context = self.reply_context.get()
        if context is None:
            raise RuntimeError('VK response requires a durable event context')
        key = f'vk-event:{context.key}:reply:{context.position}'
        context.position += 1
        return key

    async def queue_response(self, connection: Any, settings: Any, chat_id: int, text: Any, markup: Any = None,
                             *, frozen_chunks: list | None = None) -> int:
        """Сохранить готовый ответ интерфейса в очередь до того, как событие будет помечено обработанным."""
        key = self._key()
        chat = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='vk'", chat_id)
        if not chat:
            raise UserError('forbidden')
        if frozen_chunks is None:
            chunks = illustrations.chunks(text, getattr(text, 'image_id', None), min(3600, settings.max_message_length))
            chunks = await message_languages.prepare(connection, text, chunks, chat, source_key=key)
            prepared: list = []
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get('card_id'):
                    prepared.append(chunk)
                elif isinstance(chunk, str):
                    prepared.extend(dict(kind='vk_text', text=part) for part in split_message(chunk, 3600))
                else:
                    prepared.append(chunk)
            frozen_chunks = prepared
            if markup is not None and frozen_chunks and not frozen_chunks[-1].get('card_id'):
                pages = keyboard_pages(markup, locale=chat['ui_language'])
                if pages:
                    frozen_chunks[-1]['vk_keyboard'] = pages[0]
                    if len(pages) > 1:
                        frozen_chunks[-1]['vk_pages'] = pages[1:]
        translation = await bible.chat_translation(connection, chat_id)
        return await insert_payload(connection, chat, translation or {'id': None}, str(text), key,
                                    'vk_ui', {'kind': 'vk_ui'}, frozen_chunks=frozen_chunks)

    async def queue_welcome(self, connection: Any, settings: Any, chat_id: int, text: Any, markup: Any) -> int:
        """Поставить в очередь приветствие с картинкой и постоянной клавиатурой."""
        from app.bot.ui import WELCOME_PATH
        digest = hashlib.sha256(WELCOME_PATH.read_bytes()).hexdigest()
        return await self.queue_response(connection, settings, chat_id, text, markup, frozen_chunks=[
            dict(kind='vk_welcome', text=str(text), asset_sha256=digest, vk_keyboard=persistent(markup))])


def event_json(event: dict) -> str:
    """Сериализовать контекст события (для журналов и тестов)."""
    return json.dumps(event, ensure_ascii=False, default=str)
