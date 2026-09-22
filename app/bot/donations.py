"""Opt-in, one-time Telegram Stars support with explicit terms and durable receipts."""
# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from app.services import donations

LOGGER = logging.getLogger(__name__)
router = Router(name='stars-donations')
COMMANDS = {'donate', 'donations', 'paysupport', 'terms'}
DB_TIMEOUT = 3
API_TIMEOUT = 3


def language(user: Any) -> str:
    return 'ru' if str(getattr(user, 'language_code', '') or '').lower().split('-')[0] == 'ru' else 'en'


def text(locale: str, ru: str, en: str) -> str:
    return ru if locale == 'ru' else en


def is_private(message: Any, user: Any) -> bool:
    return (message is not None and user is not None and not getattr(user, 'is_bot', False)
        and str(getattr(message.chat.type, 'value', message.chat.type)) == 'private'
        and message.chat.id == user.id)


async def locale_for(pool: Any, user: Any) -> str:
    try:
        async with asyncio.timeout(DB_TIMEOUT), pool.acquire() as connection:
            locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1', user.id)
        return 'ru' if locale == 'ru' else 'en' if locale else language(user)
    except Exception:
        return language(user)


def terms_text(locale: str, amount: int | None = None) -> str:
    heading = text(locale, '⭐ Поддержать «Библию каждый день»', '⭐ Support Bible Every Day')
    terms = text(locale,
        'Это добровольная разовая поддержка оплаты сервера и развития бота. '
        'Библия и все функции чтения остаются бесплатными. Поддержка не открывает платных возможностей. '
        'Автоматических или повторных списаний нет.\n\n'
        'Сумма указана в Telegram Stars (⭐). История: /donations. '
        'Вопрос или запрос возврата: /paysupport текст обращения и номер поддержки. '
        'Владелец рассмотрит обращение и ответит в этом чате. Возврат не происходит автоматически. '
        'Служба поддержки Telegram не сможет разрешить вопрос об этой поддержке.\n\n'
        'Нажимая «Согласен, поддержать», вы подтверждаете, что прочитали и принимаете эти условия.',
        'This is voluntary one-time support for hosting and development. '
        'The Bible and all reading features remain free. Support unlocks no paid features. '
        'There are no automatic or recurring charges.\n\n'
        'Amounts are in Telegram Stars (⭐). History: /donations. '
        'For a payment issue or refund request, send /paysupport followed by your issue and donation number. '
        'The owner will review your request and reply in this chat. A refund is not automatic. '
        'Telegram support cannot resolve issues with this donation.\n\n'
        'By selecting “Agree and support”, you confirm that you have read and accept these terms.')
    amount_line = '' if amount is None else text(locale, f'\n\nСумма: ⭐ {amount}', f'\n\nAmount: ⭐ {amount}')
    return heading + '\n\n' + terms + amount_line


def amount_value(value: str) -> int | None:
    if not re.fullmatch(r'[1-9][0-9]{0,3}', value):
        return None
    amount = int(value)
    return amount if donations.MIN_AMOUNT <= amount <= donations.MAX_AMOUNT else None


async def send_text(bot: Any, chat_id: int, content: str, markup: Any = None) -> None:
    await bot.send_message(chat_id=chat_id, text=content, parse_mode=None,
        reply_markup=markup, request_timeout=10)


async def show_terms(bot: Any, chat_id: int, locale: str, amount: int) -> None:
    label = text(locale, f'Согласен, поддержать ⭐ {amount}', f'Agree and support ⭐ {amount}')
    await send_text(bot, chat_id, terms_text(locale, amount), InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f'donate:confirm:{amount}')]]))


async def _handle_command(message: Message, bot: Any, settings: Any, pool: Any,
                         command: str, args: str = '') -> bool:
    """Forward from the existing command handler after its standard authorization."""
    if command not in COMMANDS:
        return False
    user = message.from_user
    locale = language(user)
    tokens = (message.text or '').split(maxsplit=1)
    token = tokens[0] if tokens else ''
    if '@' in token:
        me = await bot.get_me(request_timeout=3)
        if token.rsplit('@', 1)[1].lower() != str(me.username or '').lower():
            return True
    if not is_private(message, user):
        if user is not None and not user.is_bot:
            await send_text(bot, message.chat.id, text(locale,
                'Поддержка и платежи доступны только в личном чате с ботом.',
                'Support and payments are available only in a private chat with the bot.'))
        return True
    locale = await locale_for(pool, user)
    args = args.strip()
    if command == 'terms':
        await send_text(bot, user.id, terms_text(locale))
    elif command == 'donate':
        if args:
            amount = amount_value(args)
            if amount is None:
                await send_text(bot, user.id, text(locale,
                    f'Укажите целое число от {donations.MIN_AMOUNT} до {donations.MAX_AMOUNT}: /donate 100',
                    f'Choose a whole number from {donations.MIN_AMOUNT} to {donations.MAX_AMOUNT}: /donate 100'))
            else:
                await show_terms(bot, user.id, locale, amount)
        else:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f'⭐ {amount}', callback_data=f'donate:{amount}')
                for amount in donations.PRESET_AMOUNTS[index:index+3]]
                for index in range(0, len(donations.PRESET_AMOUNTS), 3)])
            await send_text(bot, user.id, text(locale,
                '⭐ Поддержать бота\n\nБиблия бесплатна. Добровольная поддержка помогает оплачивать сервер '
                'и развивать бота. Выберите сумму или отправьте /donate 100 (1–2500 ⭐).\n\n'
                'Перед оплатой покажем условия для подтверждения. /terms · /donations · /paysupport',
                '⭐ Support the bot\n\nThe Bible is free. Voluntary support helps pay for hosting and development. '
                'Choose an amount or send /donate 100 (1–2500 ⭐).\n\n'
                'You will review and accept the terms before payment. /terms · /donations · /paysupport'), markup)
    elif command == 'donations':
        async with pool.acquire() as connection:
            rows = await donations.list_user_donations(connection, user.id, limit=10)
        lines = [text(locale, 'Ваши последние подтверждённые платежи:', 'Your latest confirmed payments:')]
        for row in rows:
            status = text(locale, 'возвращён', 'refunded') if row['status'] == 'refunded' else text(locale, 'оплачен', 'paid')
            lines.append(f"#{row['id']} · ⭐ {row['amount']} · {status}")
        if not rows:
            lines.append(text(locale, 'Платежей пока нет.', 'No payments yet.'))
        lines.append(text(locale, '\nВопрос или возврат: /paysupport текст', '\nIssue or refund request: /paysupport your message'))
        await send_text(bot, user.id, '\n'.join(lines))
    elif command == 'paysupport':
        if not args:
            await send_text(bot, user.id, text(locale,
                'Для вопроса по платежу или возврата отправьте:\n/paysupport Номер #123 и описание проблемы\n\n'
                'Обращение сохранится для владельца бота. История: /donations. '
                'Поддержка Telegram не сможет решить этот вопрос. Не присылайте пароли и данные карты.',
                'For a payment issue or refund request, send:\n/paysupport Donation #123 and your issue\n\n'
                'Your request will be saved for the bot owner. History: /donations. '
                'Telegram support cannot resolve this issue. Do not include passwords or card details.'))
        else:
            try:
                async with pool.acquire() as connection:
                    identifier = await donations.create_support_request(connection, user.id, args)
            except ValueError:
                await send_text(bot, user.id, text(locale, 'Проверьте текст обращения: от 1 до 4000 символов.',
                    'Please check your request: 1 to 4000 characters.'))
            else:
                await send_text(bot, user.id, text(locale,
                    f'Обращение #{identifier} сохранено для владельца бота. Номер сохраните. '
                    'Это подтверждение регистрации, а не подтверждение возврата.',
                    f'Request #{identifier} has been saved for the bot owner. Keep this number. '
                    'This confirms registration, not a refund.'))
    return True


async def unavailable(bot: Any, chat_id: int, locale: str) -> None:
    try:
        await send_text(bot, chat_id, text(locale,
            'Сейчас не удалось выполнить действие. Попробуйте позже. '
            'Если платёж уже прошёл, проверьте /donations или напишите /paysupport.',
            'This action is temporarily unavailable. Please try later. '
            'If payment already completed, check /donations or contact /paysupport.'))
    except Exception as exc:
        LOGGER.warning('Donation error response unavailable (%s)', type(exc).__name__)


async def handle_command(message: Message, bot: Any, settings: Any, pool: Any,
                         command: str, args: str = '') -> bool:
    if command not in COMMANDS:
        return False
    try:
        return await _handle_command(message, bot, settings, pool, command, args)
    except Exception as exc:
        LOGGER.warning('Donation command failed (%s)', type(exc).__name__)
        await unavailable(bot, message.chat.id, language(message.from_user))
        return True


async def _donation_callback(query: CallbackQuery, bot: Any, db_pool: Any) -> None:
    locale = language(query.from_user)
    valid_private = is_private(query.message, query.from_user)
    match = re.fullmatch(r'donate:(confirm:)?([1-9][0-9]{0,3})', query.data or '')
    amount = amount_value(match[2]) if match else None
    valid = valid_private and amount is not None
    if valid and match[1]:
        message = query.message
        author = getattr(message, 'from_user', None)
        markup = getattr(message, 'reply_markup', None)
        valid = bool(author and author.is_bot and author.id == bot.id
            and message.text in {terms_text('ru', amount), terms_text('en', amount)}
            and markup and any(button.callback_data == query.data
                for row in markup.inline_keyboard for button in row))
    # Acknowledge before database or invoice work so the client spinner stops.
    try:
        await bot.answer_callback_query(callback_query_id=query.id, text=None if valid else text(locale,
            'Откройте /donate в личном чате.', 'Open /donate in your private chat.'),
            show_alert=not valid, request_timeout=3)
    except TelegramAPIError:
        return
    if not valid:
        return
    async with db_pool.acquire() as connection:
        blocked = await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1', query.from_user.id)
    if blocked:
        await send_text(bot, query.from_user.id, text(locale, 'Действие недоступно.', 'This action is unavailable.'))
        return
    locale = await locale_for(db_pool, query.from_user)
    if not match[1]:
        await show_terms(bot, query.from_user.id, locale, amount)
        return
    async with db_pool.acquire() as connection:
        order = await donations.create_order(connection, query.from_user.id, amount)
    await bot.send_invoice(chat_id=query.from_user.id,
        title=text(locale, 'Поддержка Библии каждый день', 'Support Bible Every Day'),
        description=text(locale, 'Добровольная разовая поддержка сервера и развития. Библия бесплатна. '
            'Без повторных списаний. Поддержка и возврат: /paysupport. Условия: /terms.',
            'Voluntary one-time support for hosting and development. The Bible is free. '
            'No recurring charge. Support or refunds: /paysupport. Terms: /terms.'),
        payload=order['payload'], currency='XTR', provider_token='',
        prices=[LabeledPrice(label=text(locale, 'Разовая поддержка', 'One-time support'), amount=order['amount'])],
        start_parameter='donate', request_timeout=10)
    # Invoice is already delivered; do not repeat it just to clear a button.
    with suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(chat_id=query.message.chat.id,
            message_id=query.message.message_id, reply_markup=None, request_timeout=3)


@router.callback_query(F.data.startswith('donate:'))
async def donation_callback(query: CallbackQuery, bot: Any, db_pool: Any) -> None:
    try:
        await _donation_callback(query, bot, db_pool)
    except Exception as exc:
        LOGGER.warning('Donation callback failed (%s)', type(exc).__name__)
        if is_private(query.message, query.from_user):
            await unavailable(bot, query.from_user.id, language(query.from_user))


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Any, db_pool: Any) -> None:
    valid = False
    try:
        async with asyncio.timeout(DB_TIMEOUT), db_pool.acquire() as connection:
            blocked = await connection.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=$1', query.from_user.id)
            if not blocked and not query.from_user.is_bot:
                valid = await donations.validate_checkout(connection, query.invoice_payload,
                    query.from_user.id, query.currency, query.total_amount, query.id)
    except Exception as exc:
        LOGGER.warning('Donation checkout validation failed (%s)', type(exc).__name__)
    try:
        async with asyncio.timeout(API_TIMEOUT):
            await bot.answer_pre_checkout_query(pre_checkout_query_id=query.id, ok=bool(valid),
                error_message=None if valid else text(language(query.from_user),
                    'Платёж не подтверждён. Создайте новый счёт через /donate или обратитесь /paysupport.',
                    'Payment could not be confirmed. Open /donate again or contact /paysupport.'),
                request_timeout=API_TIMEOUT)
    except (TelegramAPIError, TimeoutError) as exc:
        LOGGER.warning('Donation checkout answer failed (%s)', type(exc).__name__)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Any, db_pool: Any) -> None:
    if not is_private(message, message.from_user):
        return
    payment = message.successful_payment
    async with db_pool.acquire() as connection:
        order, newly = await donations.record_payment(connection, payload=payment.invoice_payload,
            user_id=message.from_user.id, currency=payment.currency, total_amount=payment.total_amount,
            charge_id=payment.telegram_payment_charge_id)
    if newly:
        locale = await locale_for(db_pool, message.from_user)
        await send_text(bot, message.chat.id, text(locale,
            f"Спасибо за поддержку ⭐ {order['amount']}! Платёж #{order['id']} сохранён. "
            'Библия остаётся бесплатной. История: /donations · Вопросы и возврат: /paysupport',
            f"Thank you for supporting us with ⭐ {order['amount']}! Payment #{order['id']} is saved. "
            'The Bible stays free. History: /donations · Issues and refunds: /paysupport'))


@router.message(F.refunded_payment)
async def refunded_payment(message: Message, db_pool: Any) -> None:
    # Refund service messages need not have the donor in from_user. Trust the
    # authenticated Telegram update and verify the immutable receipt in SQL.
    payment = message.refunded_payment
    async with db_pool.acquire() as connection:
        await donations.record_refund(connection, payload=payment.invoice_payload,
            currency=payment.currency, total_amount=payment.total_amount,
            charge_id=payment.telegram_payment_charge_id)
