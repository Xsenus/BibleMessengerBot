"""Durable native orders, verified receipts and full refunds. No card data stored."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.payments.yookassa import PaymentError, https_url, identifier, minor_amount
from app.services.locks import lock_key
from app.worker.delivery import insert_payload

PRESETS = (100, 250, 500, 1000)


def validate_payment(order, payment, shop_id):
    """Only an authenticated GET/POST response can be passed to this function."""
    if (not isinstance(payment,dict) or not isinstance(payment.get('metadata'),dict) or
            not isinstance(payment.get('recipient'),dict) or
            not isinstance(payment.get('payment_method',{}),dict)):
        raise PaymentError('invalid_response')
    payment_id = identifier(payment.get('id'))
    if order['provider_payment_id'] and payment_id != order['provider_payment_id']:
        raise PaymentError('payment_mismatch')
    if (minor_amount(payment.get('amount')) != order['amount_minor'] or
            payment.get('metadata', {}).get('application') != 'bible-messenger-max' or
            payment.get('metadata', {}).get('order_id') != str(order['id']) or
            str(payment.get('recipient', {}).get('account_id')) != shop_id or
            type(payment.get('test')) is not bool):
        raise PaymentError('payment_mismatch')
    if order.get('provider_test') is not None and order['provider_test'] != payment['test']:
        raise PaymentError('payment_test_mismatch')
    status = payment.get('status')
    if status not in {'pending', 'succeeded', 'canceled'}:
        raise PaymentError('payment_status_mismatch')
    if status == 'succeeded' and payment.get('paid') is not True:
        raise PaymentError('payment_not_paid')
    method = payment.get('payment_method', {}).get('type')
    if method is not None and method != order['method']:
        raise PaymentError('payment_method_mismatch')
    return payment_id, status


async def new_order(connection, *, user_id, chat_id, rubles, method, request_key):
    if type(rubles) is not int or not 100 <= rubles <= 10000 or method not in {'bank_card', 'sbp'}:
        raise PaymentError('invalid_order')
    owner = await connection.fetchval("""SELECT EXISTS(SELECT 1 FROM telegram_chats c
        JOIN telegram_users u ON u.telegram_user_id=c.registered_by
        WHERE c.telegram_chat_id=$1 AND c.platform='max' AND c.chat_type='private'
        AND c.registered_by=$2 AND NOT u.is_blocked AND c.is_active)""", chat_id, user_id)
    if not owner:
        raise PaymentError('forbidden')
    row = await connection.fetchrow('''INSERT INTO native_payment_orders
        (id,user_id,chat_id,request_key,amount_minor,method,idempotency_key)
        VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(request_key) DO NOTHING RETURNING *''',
        uuid4(), user_id, chat_id, request_key, rubles * 100, method, uuid4())
    if row is None:
        row = await connection.fetchrow('SELECT * FROM native_payment_orders WHERE request_key=$1', request_key)
        if (row['user_id'], row['chat_id'], row['amount_minor'], row['method']) != (user_id, chat_id, rubles * 100, method):
            raise PaymentError('order_conflict')
    return row


async def notify(connection, order, *, refund=False):
    chat = await connection.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'", order['chat_id'])
    if not chat:
        raise PaymentError('destination_mismatch')
    amount = order['amount_minor'] // 100
    if chat['ui_language'] == 'ru':
        text = (f'Возврат {amount} ₽ подтверждён платёжным сервисом.' if refund else
                f'Спасибо за поддержку! Оплата {amount} ₽ подтверждена.')
        if order['provider_test']:
            text = 'ТЕСТОВЫЙ ПЛАТЁЖ — деньги не списывались.\n' + text
    else:
        text = f'Refund of {amount} RUB confirmed.' if refund else f'Thank you! Payment of {amount} RUB confirmed.'
        if order['provider_test']:
            text = 'TEST PAYMENT — no funds were charged.\n' + text
    await insert_payload(connection, chat, {'id': None}, text,
                         f"native-payment:{order['id']}:{'refund' if refund else 'paid'}", 'max_ui',
                         {'kind': 'max_ui'}, frozen_chunks=[{'kind': 'max_text', 'text': text}])


async def record_payment(connection, order_id, payment, shop_id):
    async with connection.transaction():
        order = await connection.fetchrow('SELECT * FROM native_payment_orders WHERE id=$1 FOR UPDATE', order_id)
        if not order:
            raise PaymentError('unknown_payment')
        payment_id, status = validate_payment(order, payment, shop_id)
        if order['status'] in {'succeeded', 'canceled'} and status != order['status']:
            # A stale GET result must never roll a confirmed receipt backwards.
            return order
        confirmation = payment.get('confirmation') or {}
        if not isinstance(confirmation,dict):
            raise PaymentError('invalid_confirmation')
        url = confirmation.get('confirmation_url')
        if url is not None and not https_url(url):
            raise PaymentError('invalid_checkout_url')
        order = await connection.fetchrow('''UPDATE native_payment_orders SET provider_payment_id=$2,
            status=$3,checkout_url=COALESCE($4,checkout_url),provider_test=$5,
            confirmed_at=CASE WHEN $3='succeeded' THEN COALESCE(confirmed_at,now()) ELSE confirmed_at END
            WHERE id=$1 RETURNING *''', order_id, payment_id, status, url, payment['test'])
        if status == 'succeeded' and not order['notified_at']:
            await notify(connection, order)
            await connection.execute('UPDATE native_payment_orders SET notified_at=now() WHERE id=$1', order_id)
        return order


async def checkout(connection, api, order):
    # A session advisory lock spans the network call without an open transaction.
    key = lock_key('native-payment-create', str(order['id']))
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        order = await connection.fetchrow('SELECT * FROM native_payment_orders WHERE id=$1', order['id'])
        if order['provider_payment_id']:
            payment = await api.payment(order['provider_payment_id'])
        else:
            # Provider deduplication expires at 24h. Lost responses older than this
            # need merchant reconciliation; a new POST could create a second charge.
            if datetime.now(UTC) - order['created_at'] > timedelta(hours=23):
                raise PaymentError('reconciliation_required')
            payment = await api.create(order)
        return await record_payment(connection, order['id'], payment, api.settings.shop_id)
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)


async def record_refund(connection, refund, result):
    async with connection.transaction():
        order = await connection.fetchrow('SELECT * FROM native_payment_orders WHERE id=$1 FOR UPDATE', refund['order_id'])
        current = await connection.fetchrow('SELECT * FROM native_payment_refunds WHERE order_id=$1 FOR UPDATE', refund['order_id'])
        rid = identifier(result.get('id'))
        if (not order or not current or order['status'] != 'succeeded' or
                result.get('payment_id') != order['provider_payment_id'] or
                minor_amount(result.get('amount')) != order['amount_minor'] or
                result.get('status') not in {'pending', 'succeeded', 'canceled'} or
                (current['provider_refund_id'] and current['provider_refund_id'] != rid)):
            raise PaymentError('refund_mismatch')
        status = result['status']
        if current['status'] in {'succeeded', 'canceled'} and current['status'] != status:
            return current
        current = await connection.fetchrow('''UPDATE native_payment_refunds SET provider_refund_id=$2,status=$3,
            confirmed_at=CASE WHEN $3='succeeded' THEN COALESCE(confirmed_at,now()) ELSE confirmed_at END
            WHERE order_id=$1 RETURNING *''', order['id'], rid, status)
        if status == 'succeeded' and not current['notified_at']:
            await notify(connection, order, refund=True)
            await connection.execute('UPDATE native_payment_refunds SET notified_at=now() WHERE order_id=$1', order['id'])
        return current


async def full_refund(connection, api, order_id):
    """Operator-only caller; user requests are tickets, never an automatic refund."""
    key = lock_key('native-payment-refund', str(order_id))
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        order = await connection.fetchrow('SELECT * FROM native_payment_orders WHERE id=$1', order_id)
        if not order or not order['provider_payment_id']:
            raise PaymentError('unknown_payment')
        order = await record_payment(connection, order_id, await api.payment(order['provider_payment_id']), api.settings.shop_id)
        if order['status'] != 'succeeded':
            raise PaymentError('payment_not_paid')
        refund = await connection.fetchrow('''INSERT INTO native_payment_refunds(order_id,idempotency_key)
            VALUES($1,$2) ON CONFLICT(order_id) DO UPDATE SET order_id=EXCLUDED.order_id RETURNING *''', order_id, uuid4())
        if refund['provider_refund_id']:
            result = await api.refund(refund['provider_refund_id'])
        else:
            if datetime.now(UTC) - refund['created_at'] > timedelta(hours=23):
                raise PaymentError('reconciliation_required')
            result = await api.create_refund(order, refund)
        return await record_refund(connection, refund, result)
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)


async def notification(connection, api, event):
    """Incoming statuses are hints only. Always GET the object with merchant auth."""
    if not isinstance(event, dict) or event.get('type') != 'notification':
        raise PaymentError('invalid_notification')
    kind = event.get('event')
    obj = event.get('object')
    if not isinstance(obj, dict):
        raise PaymentError('invalid_notification')
    object_id = identifier(obj.get('id'))
    if kind in {'payment.succeeded', 'payment.canceled', 'payment.waiting_for_capture'}:
        order_id = await connection.fetchval('SELECT id FROM native_payment_orders WHERE provider_payment_id=$1', object_id)
        if order_id is None:
            # This can arrive before the create response commits. Ask redelivery.
            metadata = obj.get('metadata') or {}
            if not isinstance(metadata, dict):
                raise PaymentError('invalid_notification')
            try:
                from uuid import UUID
                candidate = UUID(identifier(metadata.get('order_id')))
            except PaymentError:
                return False
            order_id = await connection.fetchval('SELECT id FROM native_payment_orders WHERE id=$1', candidate)
            if order_id is None:
                return False
        await record_payment(connection, order_id, await api.payment(object_id), api.settings.shop_id)
        return True
    if kind == 'refund.succeeded':
        refund = await connection.fetchrow('SELECT * FROM native_payment_refunds WHERE provider_refund_id=$1', object_id)
        if refund is None:
            # A refund notification can precede its create response. payment_id is
            # only used to locate an existing refund intent; GET verifies ownership.
            payment_id = identifier(obj.get('payment_id'))
            refund = await connection.fetchrow('''SELECT r.* FROM native_payment_refunds r
                JOIN native_payment_orders o ON o.id=r.order_id WHERE o.provider_payment_id=$1''', payment_id)
        if refund is None:
            return False
        await record_refund(connection, refund, await api.refund(object_id))
        return True
    return False
