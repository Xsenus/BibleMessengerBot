"""Мост MAX: права и модели MAX за тем же интерфейсом, что использует общий обработчик команд. DTO aiogram — лишь внутренняя форма команд; API Telegram для MAX не вызывается, а вся публикация идёт через общую очередь и отправитель MAX."""
from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Chat, User

from app.maxbot.client import MaxAPIError, MaxClient
from app.maxbot.identities import external, lookup
from app.maxbot.transport import MaxSender
from app.maxbot.ui import keyboard_attachment
from app.services import bible, illustrations, message_languages
from app.services.errors import UserError
from app.services.formatting import split_wire_message
from app.worker.delivery import insert_payload

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplyContext:
    """Контекст ответов на одно событие: ключ события и счётчик ответов (для стабильных ключей идемпотентности)."""
    key: str
    position: int = 0


class MaxBotBridge:
    """Объект, который притворяется ботом aiogram для общих обработчиков: отвечает на `get_me`, `get_chat`, `get_chat_member`, подтверждает нажатия и ставит ответы в очередь."""
    platform = 'max'

    def __init__(self, client: MaxClient, pool: Any, info: dict):
        """Запомнить клиент MAX, пул БД и сведения о боте (его user_id)."""
        self.client, self.pool = client, pool
        self.id = info['user_id']
        self.info = info
        self.reply_context: ContextVar[ReplyContext | None] = ContextVar('max_reply_context', default=None)

    def sender(self, connection):
        """Создать отправителя MAX для указанного соединения."""
        return MaxSender(self.client, self.id, connection)

    async def get_me(self):
        """Данные самого бота (id и имя) в виде, понятном общим обработчикам."""
        return SimpleNamespace(id=self.id, username=self.info.get('username'))

    async def get_chat(self, identifier):
        """Найти чат MAX по внутреннему идентификатору (строки Telegram не раскрываются); для групп и каналов проверяет, что чат у MAX активен."""
        if type(identifier) is not int:
            raise UserError('no_result')
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'", identifier)
            if not row:
                mapped = await lookup(connection, 'chat', identifier)
                row = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'", mapped) if mapped is not None else None
            if not row:
                raise UserError('forbidden')
            identifier = row['telegram_chat_id']
            real = await external(connection, 'chat', identifier)
        if row['chat_type'] == 'private':
            return Chat(id=identifier, type='private', title=row['title'])
        try:
            live = await self.client.request('GET', f'/chats/{real}')
        except MaxAPIError:
            raise UserError('forbidden') from None
        if live.get('status') != 'active' or live.get('type') not in {'chat', 'channel'}:
            raise UserError('forbidden')
        return Chat(id=identifier, type='channel' if live['type'] == 'channel' else 'group',
                    title=live.get('title'), is_forum=False)

    async def get_chat_member(self, chat_id: int, user_id: int):
        """Получить роль участника в чате MAX и права публикации бота: создатель, администратор или участник."""
        async with self.pool.acquire() as connection:
            real_chat = await external(connection, 'chat', chat_id)
            real_actor = self.id if user_id == self.id else await external(connection, 'user', user_id)
        try:
            if user_id == self.id:
                member = await self.client.request('GET', f'/chats/{real_chat}/members/me')
            else:
                result = await self.client.request('GET', f'/chats/{real_chat}/members',
                                                   params={'user_ids': str(real_actor)})
                member = next((m for m in result.get('members', []) if m.get('user_id') == real_actor), None)
            if not member or member.get('user_id') != real_actor:
                raise UserError('forbidden')
        except MaxAPIError:
            raise UserError('forbidden') from None
        owner = member.get('is_owner') is True
        admin = member.get('is_admin') is True
        rights = set(member.get('permissions') or [])
        can_write = owner or bool(rights & {'write', 'post_edit_delete_message'})
        # The shared authorize() checks admin status for groups, posting permission
        # for channels. Apply the MAX write requirement to both destination types.
        status = 'creator' if owner else 'administrator' if admin else 'member'
        if user_id == self.id and not can_write:
            status = 'member'
        return SimpleNamespace(status=status, can_post_messages=can_write)

    async def __call__(self, method, **_):
        """Выполнить вызов «ответ на нажатие кнопки» (единственный поддерживаемый метод): идемпотентно, не отправляет повторное чтение."""
        if not isinstance(method, AnswerCallbackQuery):
            raise UserError('invalid')
        try:
            await self.client.answer(method.callback_query_id, method.text)
        except MaxAPIError as error:
            LOGGER.warning('MAX callback acknowledgement deferred: %s', error.code)
        return True

    def _key(self):
        """Выдать следующий ключ идемпотентности ответа для текущего события; вне контекста события — ошибка."""
        context = self.reply_context.get()
        if context is None:
            raise RuntimeError('MAX response requires a durable event context')
        key = f'max-event:{context.key}:reply:{context.position}'
        context.position += 1
        return key

    async def queue_response(self, connection, settings, chat_id, text, markup=None,
                             *, frozen_chunks=None):
        """Сохранить готовый ответ интерфейса в очередь доставки до того, как событие вебхука будет помечено обработанным: текст режется на части MAX, добавляются карточки языков и клавиатура."""
        key = self._key()
        chat = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'", chat_id)
        if not chat:
            raise UserError('forbidden')
        if frozen_chunks is None:
            chunks = illustrations.chunks(text, getattr(text, 'image_id', None), min(3600, settings.max_message_length))
            chunks = await message_languages.prepare(connection, text, chunks, chat, source_key=key)
            prepared = []
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get('card_id'):
                    prepared.append(chunk)
                    continue
                if isinstance(chunk, str):
                    prepared.extend(dict(kind='max_text', text=part) for part in split_wire_message(chunk))
                else:
                    prepared.append(chunk)
            frozen_chunks = prepared
            if markup is not None and frozen_chunks and not frozen_chunks[-1].get('card_id'):
                frozen_chunks[-1]['max_keyboard'] = keyboard_attachment(markup)
        translation = await bible.chat_translation(connection, chat_id)
        return await insert_payload(connection, chat, translation or {'id': None}, str(text), key,
                                    'max_ui', {'kind': 'max_ui'}, frozen_chunks=frozen_chunks)

    async def queue_welcome(self, connection, settings, chat_id, text, markup):
        """Поставить в очередь приветствие с картинкой и клавиатурой (хеш изображения защищает от подмены)."""
        from app.bot.ui import WELCOME_PATH
        digest = hashlib.sha256(WELCOME_PATH.read_bytes()).hexdigest()
        await self.queue_response(connection, settings, chat_id, text, markup, frozen_chunks=[
            dict(kind='max_welcome', text=str(text), asset_sha256=digest,
                 max_keyboard=keyboard_attachment(markup)),
        ])


def user_model(user: dict, internal_id: int, locale: str) -> User:
    """Собрать модель пользователя aiogram из данных MAX и внутреннего ID."""
    return User(id=internal_id, is_bot=bool(user.get('is_bot')), first_name=user.get('first_name') or 'Читатель',
                last_name=user.get('last_name'), username=user.get('username'), language_code=locale)
