"""Private native support menu, checkout links, receipts and refund tickets."""
# ruff: noqa: RUF001
from __future__ import annotations

import html
import re
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.payments import ledger
from app.payments.yookassa import MerchantSettings, PaymentError, YooKassa
from app.services.errors import UserError


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=payload) for text, payload in row] for row in rows])


async def support(dispatcher, message, parsed):
    if message.chat.type != 'private':
        raise UserError('private_only')
    settings = MerchantSettings.from_env()
    async with dispatcher.pool.acquire() as connection:
        row = await connection.fetchrow("""SELECT c.ui_language,u.is_blocked FROM telegram_chats c
            JOIN telegram_users u ON u.telegram_user_id=c.registered_by
            WHERE c.telegram_chat_id=$1 AND c.registered_by=$2 AND c.platform='max'""",
            message.chat.id, message.from_user.id)
        if not row or row['is_blocked']:
            raise UserError('forbidden')
        ru = row['ui_language'] == 'ru'
        args = list(parsed.arguments)
        if parsed.name == 'terms':
            contact = html.escape(settings.support_contact or '/paysupport')
            text = ('Чтение Библии бесплатно. Поддержка — добровольный разовый платёж в рублях через ЮKassa, '
                    'картой или СБП. Повторных списаний и сохранения карты нет. Данные карты вводятся на странице '
                    f'платёжного сервиса. Поддержка и запрос возврата: {contact}; /paysupport с описанием вопроса.' if ru else
                    'Bible reading is free. Support is an optional one-time RUB payment by card or SBP via YooKassa. '
                    'No recurring charges or saved cards. Card details are entered on the provider page. '
                    f'Support and refund requests: {contact}; /paysupport followed by your question.')
            await dispatcher._response(message.chat, text)
            return
        if parsed.name == 'paysupport':
            if args:
                text = ' '.join(args)
                if len(text) > 4000:
                    raise UserError('invalid')
                request_key = 'max-support:' + dispatcher.bridge.reply_context.get().key
                ticket = await connection.fetchval('''INSERT INTO native_payment_support(user_id,request_key,message)
                    VALUES($1,$2,$3) ON CONFLICT(request_key) DO UPDATE SET request_key=EXCLUDED.request_key RETURNING id''',
                    message.from_user.id, request_key, text)
                answer = f'Обращение №{ticket} сохранено.' if ru else f'Support ticket #{ticket} saved.'
            else:
                answer = ('Напишите /paysupport и ваш вопрос или просьбу о возврате. История: /donations.' if ru else
                          'Use /paysupport followed by your question or refund request. Receipts: /donations.')
            if settings.support_contact:
                answer += '\n' + html.escape(settings.support_contact)
            await dispatcher._response(message.chat, answer)
            return
        if parsed.name == 'donations':
            rows = await connection.fetch('''SELECT o.*,r.status AS refund_status FROM native_payment_orders o
                LEFT JOIN native_payment_refunds r ON r.order_id=o.id
                WHERE o.user_id=$1 AND o.status='succeeded' ORDER BY o.confirmed_at DESC LIMIT 10''', message.from_user.id)
            text = 'Ваша поддержка:' if ru else 'Your receipts:'
            for row in rows:
                test = '[ТЕСТ] ' if ru and row['provider_test'] else '[TEST] ' if row['provider_test'] else ''
                state = (' — возвращён' if ru else ' — refunded') if row['refund_status'] == 'succeeded' else ''
                text += f"\n{test}{row['amount_minor']//100} ₽ · {row['confirmed_at']:%Y-%m-%d}{state}\n<code>{row['id']}</code>"
            if not rows:
                text = 'Подтверждённых платежей пока нет.' if ru else 'No confirmed payments yet.'
            await dispatcher._response(message.chat, text)
            return
        if parsed.name == 'refund':
            await dispatcher._response(message.chat, 'Запрос возврата: /paysupport с номером платежа из /donations.' if ru else
                                       'Request a refund with /paysupport and the receipt ID from /donations.')
            return
        if args and not (parsed.name == 'start' and args == ['donate']):
            if len(args) != 2 or not re.fullmatch(r'[1-9][0-9]{2,4}', args[0]) or args[1] not in {'card', 'sbp'}:
                await dispatcher._response(message.chat, '/donate 100 card · /donate 100 sbp (100–10000 ₽)')
                return
            await checkout(dispatcher, message, int(args[0]), 'bank_card' if args[1] == 'card' else 'sbp')
            return
    if not settings.enabled:
        await dispatcher._response(message.chat, 'Оплата картой и СБП появится после подключения магазина. Чтение бесплатно.' if ru else
                                   'Card and SBP support will be available when the merchant account is connected. Reading is free.')
        return
    rows = [[(f'{amount} ₽ · '+('Карта' if ru else 'Card'), f'maxpay:{amount}:bank_card'),
             (f'{amount} ₽ · '+('СБП' if ru else 'SBP'), f'maxpay:{amount}:sbp')] for amount in ledger.PRESETS]
    await dispatcher._response(message.chat, 'Поддержать «Библию каждый день»\nВыберите сумму и способ оплаты. '
                               'Это разовая добровольная поддержка; чтение бесплатно. Условия: /terms. История: /donations.' if ru else
                               'Support Bible Every Day\nChoose an amount and payment method. One-time optional support; '
                               'reading is free. Terms: /terms. Receipts: /donations.', buttons(rows))


async def checkout(dispatcher, message, rubles=None, method=None, *, retry_id=None):
    if message.chat.type != 'private':
        raise UserError('private_only')
    config = MerchantSettings.from_env()
    order = None
    api = None
    async with dispatcher.pool.acquire() as connection:
        locale = await connection.fetchval('SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1', message.chat.id)
        ru = locale == 'ru'
        try:
            api = YooKassa(config)
            if retry_id is None:
                order = await ledger.new_order(connection, user_id=message.from_user.id, chat_id=message.chat.id,
                                               rubles=rubles, method=method,
                                               request_key='max-pay:' + dispatcher.bridge.reply_context.get().key)
            else:
                order = await connection.fetchrow('''SELECT o.* FROM native_payment_orders o
                    JOIN telegram_users u ON u.telegram_user_id=o.user_id
                    JOIN telegram_chats c ON c.telegram_chat_id=o.chat_id
                    WHERE o.id=$1 AND o.user_id=$2 AND o.chat_id=$3 AND NOT u.is_blocked
                    AND c.platform='max' AND c.chat_type='private' AND c.is_active''',
                    UUID(retry_id), message.from_user.id, message.chat.id)
                if not order:
                    raise UserError('forbidden')
            order = await ledger.checkout(connection, api, order)
            if order['status'] == 'succeeded':
                text = 'Платёж уже подтверждён. История: /donations.' if ru else 'Payment confirmed. Receipts: /donations.'
                markup = None
            elif order['status'] == 'canceled':
                text = 'Платёж отменён. Новый платёж: /donate.' if ru else 'Payment canceled. Create a new one: /donate.'
                markup = None
            elif order['checkout_url']:
                text = f"Разовая поддержка: {order['amount_minor']//100} ₽.\n" + ('Оплатите на защищённой странице ЮKassa. '
                        'После подтверждения бот пришлёт квитанцию. /terms · /paysupport' if ru else
                        'Pay on the secure YooKassa page. A receipt follows confirmation. /terms · /paysupport')
                if order['provider_test']:
                    text = ('ТЕСТОВЫЙ ПЛАТЁЖ\n' if ru else 'TEST PAYMENT\n') + text
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text='Перейти к оплате' if ru else 'Pay', url=order['checkout_url'])]])
            else:
                raise PaymentError('missing_checkout_url')
        except PaymentError as error:
            if error.code == 'forbidden':
                raise UserError('forbidden') from None
            text = ('Оплата пока недоступна. Деньги не подтверждены; история: /donations. '
                    'Поддержка: /paysupport.' if ru else 'Checkout is unavailable. No payment has been confirmed; '
                    'receipts: /donations. Support: /paysupport.')
            markup = buttons([[('Повторить' if ru else 'Retry', 'maxretry:' + str(order['id']))]]) if order else None
        finally:
            if api:
                await api.close()
    await dispatcher._response(message.chat, text, markup)
