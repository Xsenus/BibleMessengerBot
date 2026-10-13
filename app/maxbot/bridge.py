"""Мост MAX: права и модели MAX за тем же интерфейсом, что использует общий обработчик команд. DTO aiogram — лишь внутренняя форма команд; API Telegram для MAX не вызывается, а вся публикация идёт через общую очередь и отправитель MAX."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Chat, User

from app.maxbot.client import MaxAPIError, MaxClient
from app.maxbot.identities import external, lookup
from app.maxbot.transport import MaxSender
from app.maxbot.ui import keyboard_attachment
from app.platforms.bridge import BridgeBase
from app.platforms.builtin import MAX
from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)


class MaxBotBridge(BridgeBase):
    """Объект, который притворяется ботом aiogram для общих обработчиков: отвечает на `get_me`, `get_chat`, `get_chat_member`, подтверждает нажатия и ставит ответы в очередь."""
    spec = MAX

    def __init__(self, client: MaxClient, pool: Any, info: dict):
        """Запомнить клиент MAX, пул БД и сведения о боте (его user_id)."""
        super().__init__(client, pool, info)
        self.id = info['user_id']

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

    def attach_keyboard(self, chunk, markup, chat):
        """Добавить в часть очереди клавиатуру MAX."""
        chunk['max_keyboard'] = keyboard_attachment(markup)

    def welcome_chunk(self, text, digest, markup):
        """Часть приветствия MAX: картинка и клавиатура."""
        return dict(kind='max_welcome', text=str(text), asset_sha256=digest, max_keyboard=keyboard_attachment(markup))


def user_model(user: dict, internal_id: int, locale: str) -> User:
    """Собрать модель пользователя aiogram из данных MAX и внутреннего ID."""
    return User(id=internal_id, is_bot=bool(user.get('is_bot')), first_name=user.get('first_name') or 'Читатель',
                last_name=user.get('last_name'), username=user.get('username'), language_code=locale)
