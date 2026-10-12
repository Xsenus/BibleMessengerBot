"""Диспетчер событий ВКонтакте: проверенные события превращаются в общие команды и проверки прав, как в Telegram."""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from aiogram.types import CallbackQuery, Chat, Message

from app.bot import handlers
from app.bot.commands import MANAGEMENT, parse_command
from app.maxbot.bridge import user_model
from app.services import bible
from app.services.chat_sources import ingest_post
from app.services.errors import UserError
from app.services.i18n import tr
from app.services.locks import chat_lock
from app.platforms.bridge import ReplyContext
from app.vkbot.bridge import CONVERSATION_BASE, VkBotBridge
from app.vkbot.client import VkAPIError
from app.vkbot.identities import lookup, register
from app.worker.delivery import insert_payload

NAVIGATION = {'/next', '/today', '/random', '/search', '/settings', '/help', '/daily', '/start', '/menu', '/donate'}
MENTION = re.compile(r'^\s*\[club\d+\|[^\]]*\]\s*,?\s*')
START_WORDS = {'начать', 'старт', 'start', 'привет'}
PROFILE_TTL = 6 * 3600


class VkDispatcher:
    """Связывает события VK с общими обработчиками: регистрирует пользователей и чаты, превращает сообщения и нажатия
    в сообщения/запросы aiogram и вызывает те же обработчики."""

    def __init__(self, bridge: VkBotBridge, pool: Any, settings: Any) -> None:
        self.bridge, self.pool, self.settings = bridge, pool, settings
        self._profiles: dict[int, tuple[float, dict]] = {}
        self._titles: dict[int, tuple[float, str]] = {}

    async def _profile(self, user_id: int) -> dict:
        """Профиль пользователя VK (имя, адрес страницы); кэшируется на 6 часов, при сбое — только номер."""
        cached = self._profiles.get(user_id)
        if cached and time.monotonic() - cached[0] < PROFILE_TTL:
            return cached[1]
        profile = {'user_id': user_id}
        try:
            users = await self.bridge.client.call('users.get', user_ids=user_id, fields='screen_name')
            if users:
                profile.update(first_name=users[0].get('first_name'), last_name=users[0].get('last_name'),
                               username=users[0].get('screen_name'))
        except VkAPIError:
            pass
        self._profiles[user_id] = (time.monotonic(), profile)
        return profile

    async def _title(self, peer_id: int) -> str | None:
        """Название беседы (кэшируется); для личного диалога — None."""
        if peer_id < CONVERSATION_BASE:
            return None
        cached = self._titles.get(peer_id)
        if cached and time.monotonic() - cached[0] < PROFILE_TTL:
            return cached[1]
        title = None
        try:
            result = await self.bridge.client.call('messages.getConversationsById', peer_ids=peer_id, group_id=self.bridge.id)
            title = ((result.get('items') or [{}])[0].get('chat_settings') or {}).get('title')
        except VkAPIError:
            pass
        self._titles[peer_id] = (time.monotonic(), title or 'VK')
        return title

    async def _context(self, user_id: int, peer_id: int) -> tuple[Any, Chat]:
        """Зарегистрировать пользователя и чат (внутренние идентификаторы) и вернуть модели для обработчиков."""
        profile = await self._profile(user_id)
        chat_type = 'private' if peer_id < CONVERSATION_BASE else 'group'
        title = await self._title(peer_id)
        async with self.pool.acquire() as connection:
            actor, chat = await register(connection, user=profile, external_chat_id=peer_id, chat_type=chat_type,
                                         timezone=self.settings.default_timezone, locale='ru', title=title)
        return user_model(profile, actor, 'ru'), Chat(id=chat, type=chat_type)

    async def _response(self, chat: Chat, text: str) -> None:
        """Поставить в очередь ответ пользователю в указанный чат."""
        async with self.pool.acquire() as connection:
            await self.bridge.queue_response(connection, self.settings, chat.id, text, None)

    def _message(self, chat: Chat, actor: Any, cmid: int, text: str | None) -> Message:
        return Message(message_id=cmid, date=datetime.now(UTC), chat=chat, from_user=actor, text=text).as_(self.bridge)

    async def _command(self, message: Message) -> None:
        """Выполнить команду из сообщения; `/help` и `/thread` обрабатываются здесь, остальное — общим `command_handler`."""
        try:
            parsed = parse_command(message.text or '')
        except UserError:
            await handlers.command_handler(message, self.bridge, self.pool, self.settings)
            return
        tokens = (message.text or '').split()
        if tokens and tokens[-1].startswith('chat:'):
            name = tokens[0].split('@', 1)[0].lstrip('/').lower()
            if name not in MANAGEMENT or not re.fullmatch(r'chat:-?[1-9][0-9]{0,18}', tokens[-1]):
                raise UserError('invalid')
            async with self.pool.acquire() as connection:
                target = await lookup(connection, 'chat', int(tokens[-1][5:]))
                private = await connection.fetchval("SELECT chat_type='private' FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='vk'", target) if target else True
            if target is None or private:
                raise UserError('forbidden')
            message = message.model_copy(update={'text': ' '.join([*tokens[:-1], str(target)])})
        if parsed.name == 'thread':
            await self._response(message.chat, 'Темы форума Telegram недоступны для ВКонтакте.')
            return
        await handlers.command_handler(message, self.bridge, self.pool, self.settings)

    async def dispatch(self, event: dict, event_key: str) -> None:
        """Обработать одно событие: установить контекст ответов, выполнить его и показать ошибку пользователя в чате."""
        token = self.bridge.reply_context.set(ReplyContext(event_key))
        chat_holder: list[Chat] = []
        try:
            await self._dispatch(event, chat_holder)
        except UserError as error:
            if not chat_holder:
                raise
            async with self.pool.acquire() as connection:
                locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1', chat_holder[0].id) or 'ru'
            await self._response(chat_holder[0], tr(locale, error.key))
        finally:
            self.bridge.reply_context.reset(token)

    async def _dispatch(self, event: dict, chat_holder: list) -> None:
        kind, obj = event['type'], event['object']
        if kind == 'message_new':
            await self._message_new(obj['message'], chat_holder)
        elif kind == 'message_event':
            await self._message_event(obj, chat_holder)
        elif kind in {'message_allow', 'message_deny'}:
            await self._allow(obj, kind == 'message_allow')
        elif kind == 'wall_post_new':
            async with self.pool.acquire() as connection:
                await ingest_post(connection, 'vk', obj.get('owner_id'), str(obj.get('id')), obj.get('text') or '', dict(
                    attachments=[a.get('type') for a in obj.get('attachments', [])]), posted_at=obj.get('date'))

    async def _allow(self, obj: dict, allowed: bool) -> None:
        """Пользователь разрешил или запретил сообщения от сообщества: чат включается или отключается вместе с подписками."""
        user_id = obj.get('user_id')
        if type(user_id) is not int:
            return
        if allowed:
            await self._context(user_id, user_id)
            async with self.pool.acquire() as connection:
                chat = await lookup(connection, 'chat', user_id)
                if chat:
                    await connection.execute("UPDATE telegram_chats SET is_active=true WHERE telegram_chat_id=$1 AND platform='vk'", chat)
            return
        async with self.pool.acquire() as connection:
            chat = await lookup(connection, 'chat', user_id)
            if chat is not None:
                async with chat_lock(connection, chat), connection.transaction():
                    await connection.execute("UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1 AND platform='vk'", chat)
                    await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1', chat)

    async def _message_new(self, message: dict, chat_holder: list) -> None:
        """Новое сообщение: добавление или удаление бота в беседе, команда, адрес стиха или подсказка меню."""
        sender, peer = message.get('from_id'), message.get('peer_id')
        if type(sender) is not int or type(peer) is not int:
            return
        action = message.get('action') or {}
        if sender < 0:
            return                                                   # собственные сообщения сообщества
        if action.get('type') == 'chat_kick_user' and action.get('member_id') == -self.bridge.id:
            async with self.pool.acquire() as connection:
                chat = await lookup(connection, 'chat', peer)
                if chat is not None:
                    async with chat_lock(connection, chat), connection.transaction():
                        await connection.execute("UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1 AND platform='vk'", chat)
                        await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1', chat)
            return
        actor, chat = await self._context(sender, peer)
        chat_holder.append(chat)
        if action.get('type') == 'chat_invite_user' and action.get('member_id') == -self.bridge.id:
            await handlers.authorize(self.bridge, chat, actor.id)
            return
        text = MENTION.sub('', message.get('text') or '').strip()
        cmid = message.get('conversation_message_id')
        if type(cmid) is not int or not text:
            return
        incoming = self._message(chat, actor, cmid, text)
        if text.lower() in START_WORDS and chat.type == 'private':
            await self._command(incoming.model_copy(update={'text': '/start'}))
        elif text.startswith('/'):
            await self._command(incoming)
        elif chat.type == 'private':
            from app.bot.ui import keyboard_command
            command = keyboard_command(text)
            if command is None:
                command = '/read ' + text
            await self._command(incoming.model_copy(update={'text': command}))
        else:
            async with self.pool.acquire() as connection:
                await ingest_post(connection, 'vk', peer, str(cmid), text, {}, posted_at=message.get('date'))

    async def _message_event(self, obj: dict, chat_holder: list) -> None:
        """Нажатие callback-кнопки на сообщении бота: навигация, языки, настройки, меню администратора, озвучка."""
        user_id, peer, event_id = obj.get('user_id'), obj.get('peer_id'), obj.get('event_id')
        cmid = obj.get('conversation_message_id')
        raw_payload = obj.get('payload')
        payload: dict = raw_payload if isinstance(raw_payload, dict) else {}
        raw_data = payload.get('d')
        data: str = raw_data if isinstance(raw_data, str) else ''
        if type(user_id) is not int or type(peer) is not int or type(cmid) is not int or not event_id:
            return
        token = self.bridge.event_context.set({'event_id': event_id, 'user_id': user_id, 'peer_id': peer})
        try:
            actor, chat = await self._context(user_id, peer)
            chat_holder.append(chat)
            bot_user = user_model({'user_id': self.bridge.id, 'is_bot': True, 'first_name': 'Bot'}, self.bridge.id, 'ru')
            original = self._message(chat, bot_user, cmid, None)
            callback = CallbackQuery(id=str(event_id), from_user=actor, message=original, chat_instance=str(peer), data=data).as_(self.bridge)
            if data.startswith('vkcmd:'):
                command = data[6:]
                if command not in NAVIGATION:
                    raise UserError('invalid')
                await callback.answer()
                await self._command(self._message(chat, actor, cmid, command))
            elif data.startswith('lc:'):
                await handlers.message_language_handler(callback, self.bridge, self.pool, self.settings)
            elif data.startswith('v1:'):
                await handlers.callback_handler(callback, self.bridge, self.pool, self.settings)
            elif data.startswith('vkpg:'):
                await callback.answer()
                if not re.fullmatch(r'vkpg:[0-9]{1,2}', data):
                    raise UserError('invalid')
                async with self.pool.acquire() as connection:
                    stored = await connection.fetchrow('SELECT body,attachment,pages FROM vk_keyboard_pages WHERE chat_id=$1 AND cmid=$2', chat.id, cmid)
                    pages = (json.loads(stored['pages']) if isinstance(stored['pages'], str) else stored['pages']) if stored else []
                    number = int(data[5:])
                    if number >= len(pages):
                        raise UserError('invalid')
                    from app.vkbot.rate_limit import wait_send_slot
                    await wait_send_slot(connection, chat.id)
                await self.bridge.client.edit(peer, cmid, message=stored['body'], attachment=stored['attachment'] or None, keyboard=pages[number])
            elif data.startswith('vkaudio:'):
                await callback.answer()
                await handlers.authorize(self.bridge, chat, actor.id)
                match = re.fullmatch(r'vkaudio:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})', data)
                if not match:
                    raise UserError('invalid')
                async with self.pool.acquire() as connection, chat_lock(connection, chat.id), connection.transaction():
                    if await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1', actor.id):
                        raise UserError('forbidden')
                    card = await connection.fetchrow('''SELECT * FROM reading_cards WHERE id=$1 AND telegram_chat_id=$2
                        AND telegram_message_id=$3 AND audio_id=$4 AND platform='vk' ''', int(match[1]), chat.id, cmid, int(match[2]))
                    if not card:
                        raise UserError('invalid')
                    edition = await bible.find_translation(connection, card['selected_translation_id'])
                    destination = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', chat.id)
                    await insert_payload(connection, destination, edition, '', self.bridge._key(), 'vk_audio', {'kind': 'vk_audio'},
                                         frozen_chunks=[dict(kind='vk_audio', card_id=card['id'], audio_id=card['audio_id'])])
            else:
                await callback.answer(text=tr('ru', 'invalid'))
        finally:
            self.bridge.event_context.reset(token)

