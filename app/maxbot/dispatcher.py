"""Dispatch validated MAX events through shared Bible commands and permissions."""
# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from datetime import UTC, datetime

from aiogram.types import CallbackQuery, Chat, Message

from app.bot import handlers
from app.bot.commands import MANAGEMENT, parse_command
from app.maxbot.bridge import ReplyContext, user_model
from app.maxbot.identities import lookup, register, remember_message
from app.services import bible, passage
from app.services.errors import UserError
from app.services.i18n import initial_ui, tr
from app.services.locks import chat_lock
from app.worker.delivery import insert_payload

NAVIGATION = {'/next','/today','/random','/search','/settings','/help','/daily','/donate','/start','/menu'}


class MaxDispatcher:
    def __init__(self, bridge, pool, settings):
        self.bridge, self.pool, self.settings = bridge, pool, settings
        self.destination_context = ContextVar('max_destination_context',default=None)

    async def _context(self, user, external_chat, chat_type, locale):
        async with self.pool.acquire() as connection:
            actor, chat = await register(connection, user=user, external_chat_id=external_chat,
                                          chat_type=chat_type, timezone=self.settings.default_timezone,
                                          locale=locale)
        model = Chat(id=chat,type=chat_type)
        self.destination_context.set(model)
        return user_model(user,actor,locale),model

    async def _message_id(self, chat, mid):
        async with self.pool.acquire() as connection:
            identifier = await remember_message(connection, self.bridge.id, chat, mid)
        if identifier is None:
            raise UserError('forbidden')
        return identifier

    async def _response(self, chat, text, markup=None):
        async with self.pool.acquire() as connection:
            await self.bridge.queue_response(connection,self.settings,chat.id,text,markup)

    async def _command(self, message):
        try:
            parsed = parse_command(message.text or '')
        except UserError:
            await handlers.command_handler(message,self.bridge,self.pool,self.settings)
            return
        if parsed.mentioned_bot and parsed.mentioned_bot.lower()!=(self.bridge.info.get('username') or '').lower():
            return
        # MAX chat IDs may be positive. An explicit chat:<ID> avoids confusing
        # ordinary command arguments (time, amount, page) with destinations.
        tokens = (message.text or '').split()
        if tokens and tokens[-1].startswith('chat:'):
            name = tokens[0].split('@',1)[0].lstrip('/').lower()
            if name not in MANAGEMENT or not re.fullmatch(r'chat:-?[1-9][0-9]{0,18}',tokens[-1]):
                raise UserError('invalid')
            async with self.pool.acquire() as connection:
                target = await lookup(connection,'chat',int(tokens[-1][5:]))
                private = await connection.fetchval("SELECT chat_type='private' FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'",target) if target else True
            if target is None or private:
                raise UserError('forbidden')
            message = message.model_copy(update={'text':' '.join([*tokens[:-1],str(target)])})
            parsed = parse_command(message.text or '')
        if parsed.name in {'donate','donations','paysupport','terms','refund'} or (parsed.name=='start' and parsed.arguments==('donate',)):
            from app.maxbot.payments import support
            await support(self, message, parsed)
            return
        if parsed.name == 'thread':
            await self._response(message.chat, 'Темы форума Telegram недоступны для MAX. Рассылка в этот чат: /subscribe.')
            return
        if parsed.name == 'help':
            from app.maxbot.help import help_text
            async with self.pool.acquire() as connection:
                locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',message.chat.id) or 'ru'
            await self._response(message.chat,help_text(locale))
            return
        if parsed.name == 'chats':
            if message.chat.type != 'private':
                raise UserError('private_only')
            from app.maxbot.identities import external
            from app.services.formatting import escape
            async with self.pool.acquire() as connection:
                rows = await connection.fetch("SELECT telegram_chat_id FROM telegram_chats WHERE platform='max' AND chat_type<>'private' AND is_active ORDER BY updated_at DESC LIMIT 20")
                locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',message.chat.id) or 'ru'
            lines = ['Ваши группы и каналы:' if locale=='ru' else 'Your groups and channels:']
            for row in rows:
                try:
                    chat = await self.bridge.get_chat(row['telegram_chat_id'])
                    await handlers.authorize(self.bridge,chat,message.from_user.id)
                except UserError:
                    continue
                async with self.pool.acquire() as connection:
                    real = await external(connection,'chat',chat.id)
                lines.append(f'{escape(chat.title or "MAX")}\n<code>/settings chat:{real}</code>')
            if len(lines)==1:
                lines.append('Добавьте бота администратором с правом писать сообщения.' if locale=='ru' else 'Add the bot as an administrator with posting permission.')
            await self._response(message.chat,'\n\n'.join(lines))
            return
        await handlers.command_handler(message,self.bridge,self.pool,self.settings)

    async def dispatch(self, event, event_key):
        token = self.bridge.reply_context.set(ReplyContext(event_key))
        destination_token = self.destination_context.set(None)
        try:
            await self._dispatch(event)
        except UserError as error:
            chat = self.destination_context.get()
            if chat is None:
                raise
            async with self.pool.acquire() as connection:
                locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1',chat.id) or 'ru'
            await self._response(chat,tr(locale,error.key))
        finally:
            self.destination_context.reset(destination_token)
            self.bridge.reply_context.reset(token)

    async def _dispatch(self, event):
        kind = event['update_type']
        locale = initial_ui(event.get('user_locale'))
        if kind in {'bot_stopped','bot_removed','dialog_removed'}:
            async with self.pool.acquire() as connection:
                chat = await lookup(connection,'chat',event['chat_id'])
                if chat is not None:
                    async with chat_lock(connection,chat),connection.transaction():
                        await connection.execute("UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1 AND platform='max'",chat)
                        await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1',chat)
            return
        if kind in {'chat_title_changed','bot_admin_permissions_changed'}:
            async with self.pool.acquire() as connection:
                chat_id = await lookup(connection,'chat',event['chat_id'])
            if chat_id is None:
                return
            try:
                chat = await self.bridge.get_chat(chat_id)
                member = await self.bridge.get_chat_member(chat_id,self.bridge.id)
                active = member.status in {'creator','administrator'} and member.can_post_messages
            except UserError:
                active = False
                chat = None
            async with self.pool.acquire() as connection,chat_lock(connection,chat_id),connection.transaction():
                await connection.execute('UPDATE telegram_chats SET is_active=$2,title=COALESCE($3,title) WHERE telegram_chat_id=$1 AND platform=\'max\'',chat_id,active,chat.title if chat else None)
                if not active:
                    await connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1',chat_id)
            return
        if kind in {'bot_started','bot_added'}:
            source_user = event['user']
            if source_user.get('is_bot'):
                return
            chat_type = 'private' if kind=='bot_started' else 'channel' if event.get('is_channel') else 'group'
            actor, chat = await self._context(source_user,event['chat_id'],chat_type,locale)
            if kind=='bot_added':
                await handlers.authorize(self.bridge,chat,actor.id)
                return
            mid = 'start.'+hashlib.sha256(self.bridge.reply_context.get().key.encode()).hexdigest()
            identifier = await self._message_id(chat.id,mid)
            message = Message(message_id=identifier,date=datetime.now(UTC),chat=chat,from_user=actor,text='/start').as_(self.bridge)
            await self._command(message)
            return
        if kind=='message_created':
            incoming = event['message']
            source_user = incoming.get('sender')
            content = (incoming.get('body') or {}).get('text')
            if not source_user or source_user.get('is_bot') or not content:
                return
            recipient = incoming['recipient']
            chat_type = {'dialog':'private','chat':'group','channel':'channel'}[recipient['chat_type']]
            actor, chat = await self._context(source_user,recipient['chat_id'],chat_type,locale)
            identifier = await self._message_id(chat.id,incoming['body']['mid'])
            message = Message(message_id=identifier,date=datetime.now(UTC),chat=chat,from_user=actor,text=content).as_(self.bridge)
            if content.startswith('/'):
                await self._command(message)
            elif chat_type=='private':
                from app.bot.ui import keyboard_command
                command = keyboard_command(content)
                if command is None:
                    try:
                        reference = passage.parse_reference(content)
                    except UserError:
                        reference = True
                    command = '/read '+content if reference else '/menu'
                await self._command(message.model_copy(update={'text':command}))
            return
        if kind=='message_callback':
            await self.callback(event,locale)

    async def callback(self, event, locale):
        raw = event['callback']
        original = event.get('message')
        if not original or raw['user'].get('is_bot'):
            await self.bridge.client.answer(raw['callback_id'])
            return
        recipient = original['recipient']
        sender = original.get('sender') or {}
        if sender.get('user_id')!=self.bridge.id or sender.get('is_bot') is not True:
            raise UserError('forbidden')
        chat_type = {'dialog':'private','chat':'group','channel':'channel'}[recipient['chat_type']]
        actor, chat = await self._context(raw['user'],recipient['chat_id'],chat_type,locale)
        identifier = await self._message_id(chat.id,original['body']['mid'])
        message = Message(message_id=identifier,date=datetime.now(UTC),chat=chat,
                          from_user=user_model(sender,self.bridge.id,locale),text=original['body'].get('text')).as_(self.bridge)
        callback = CallbackQuery(id=raw['callback_id'],from_user=actor,message=message,
                                 chat_instance=str(recipient['chat_id']),data=raw.get('payload')).as_(self.bridge)
        payload = raw.get('payload') or ''
        if payload.startswith('maxcmd:'):
            command = payload[7:]
            if command not in NAVIGATION:
                raise UserError('invalid')
            await callback.answer()
            mid = 'callback.'+hashlib.sha256(raw['callback_id'].encode()).hexdigest()
            command_id = await self._message_id(chat.id,mid)
            await self._command(message.model_copy(update={'message_id':command_id,'from_user':actor,'text':command}))
        elif payload.startswith(('maxpay:', 'maxretry:')):
            from app.maxbot.payments import checkout
            await callback.answer()
            payer_message = message.model_copy(update={'from_user': actor})
            match = re.fullmatch(r'maxpay:([1-9][0-9]{2,4}):(bank_card|sbp)', payload)
            retry = re.fullmatch(r'maxretry:([a-f0-9-]{36})', payload)
            if match:
                await checkout(self, payer_message, int(match[1]), match[2])
            elif retry:
                await checkout(self, payer_message, retry_id=retry[1])
            else:
                raise UserError('invalid')
        elif payload.startswith('lc:'):
            await handlers.message_language_handler(callback,self.bridge,self.pool,self.settings)
        elif payload.startswith('v1:'):
            await handlers.callback_handler(callback,self.bridge,self.pool,self.settings)
        elif payload.startswith('maxaudio:'):
            await callback.answer()
            await handlers.authorize(self.bridge,chat,actor.id)
            match = re.fullmatch(r'maxaudio:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})',payload)
            if not match:
                raise UserError('invalid')
            async with self.pool.acquire() as connection,chat_lock(connection,chat.id),connection.transaction():
                if await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1',actor.id):
                    raise UserError('forbidden')
                card = await connection.fetchrow('''SELECT * FROM reading_cards WHERE id=$1 AND telegram_chat_id=$2
                    AND telegram_message_id=$3 AND audio_id=$4 AND platform='max' ''',
                    int(match[1]),chat.id,identifier,int(match[2]))
                if not card:
                    raise UserError('invalid')
                edition = await bible.find_translation(connection,card['selected_translation_id'])
                destination = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat.id)
                await insert_payload(connection,destination,edition,'',self.bridge._key(),'max_audio',{'kind':'max_audio'},
                    frozen_chunks=[dict(kind='max_audio',card_id=card['id'],audio_id=card['audio_id'])])
        else:
            await callback.answer(text=tr(locale,'invalid'))
