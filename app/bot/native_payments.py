"""Telegram entry points to the shared RUB ledger; Stars remain independent."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from aiogram import F, Router

from app.maxbot import payments
from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)
router = Router(name='native-rub-support')


class TelegramSupport:
    def __init__(self, bot, pool, event_key):
        self.bot, self.pool = bot, pool
        self.bridge = SimpleNamespace(platform='telegram', reply_context=SimpleNamespace(
            get=lambda: SimpleNamespace(key=event_key)))

    async def _response(self, chat, text, markup=None):
        # Keep existing Stars support commands and make RUB terms/tickets explicit.
        text = text.replace('/paysupport','/paysupport card').replace('/terms','/terms card')
        await self.bot.send_message(chat_id=chat.id, text=text, parse_mode='HTML',
                                    reply_markup=markup, request_timeout=10)


async def command(message, bot, pool, name='donate', arguments=()):
    from app.bot.donations import is_private
    if not is_private(message,message.from_user):
        raise UserError('private_only')
    dispatcher = TelegramSupport(bot,pool,f'{bot.id}:{message.chat.id}:{message.message_id}')
    await payments.support(dispatcher,message,SimpleNamespace(name=name,arguments=tuple(arguments)))


@router.callback_query((F.data == 'rubmenu') | F.data.startswith('rubpay:') | F.data.startswith('rubretry:'))
async def callback(query, bot, db_pool):
    from app.bot.donations import is_private, language, unavailable
    if not is_private(query.message,query.from_user):
        await bot.answer_callback_query(callback_query_id=query.id, request_timeout=3)
        return
    sender = query.message.from_user
    offered = {button.callback_data for row in (query.message.reply_markup.inline_keyboard if query.message.reply_markup else [])
               for button in row}
    if not sender or sender.id != bot.id or not sender.is_bot or query.data not in offered:
        await bot.answer_callback_query(callback_query_id=query.id, request_timeout=3)
        return
    await bot.answer_callback_query(callback_query_id=query.id, request_timeout=3)
    dispatcher = TelegramSupport(bot,db_pool,f'{bot.id}:callback:{query.id}')
    message = SimpleNamespace(chat=query.message.chat,from_user=query.from_user)
    try:
        if query.data == 'rubmenu':
            await payments.support(dispatcher,message,SimpleNamespace(name='donate',arguments=()))
            return
        match = re.fullmatch(r'rubpay:([1-9][0-9]{2,4}):(bank_card|sbp)',query.data)
        retry = re.fullmatch(r'rubretry:([a-f0-9-]{36})',query.data)
        if match:
            await payments.checkout(dispatcher,message,int(match[1]),match[2])
        elif retry:
            await payments.checkout(dispatcher,message,retry_id=retry[1])
        else:
            raise UserError('invalid')
    except Exception as error:
        LOGGER.warning('Telegram RUB support deferred (%s)',type(error).__name__)
        await unavailable(bot,query.from_user.id,language(query.from_user))
