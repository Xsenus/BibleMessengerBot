"""Validated Stars orders and an immutable, idempotent payment/refund ledger.

Only authenticated Telegram payment updates (or authoritative reconciliation)
may call record_payment/record_refund. Pre-checkout is never payment evidence.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from app.services.locks import lock_key

MIN_AMOUNT = 1
MAX_AMOUNT = 2500
PRESET_AMOUNTS = (25, 50, 100, 250, 500)
CURRENCY = 'XTR'
_PAYLOAD = re.compile(r'donation:v1:[A-Za-z0-9_-]{32}')


def _user(user_id: int) -> None:
    if type(user_id) is not int or not 0 < user_id < 2**63:
        raise ValueError('Invalid donation user')


def _amount(amount: int, currency: str = CURRENCY) -> None:
    if type(amount) is not int or not MIN_AMOUNT <= amount <= MAX_AMOUNT or currency != CURRENCY:
        raise ValueError('Invalid Stars amount or currency')


def _payload(payload: str) -> None:
    if not isinstance(payload, str) or not _PAYLOAD.fullmatch(payload):
        raise ValueError('Invalid donation payload')


def _opaque(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or '\x00' in value or not 1 <= len(value.encode()) <= 2048:
        raise ValueError('Invalid Telegram payment identifier')


def _matches(order: Any, user_id: int, amount: int, currency: str) -> bool:
    return bool(order and order['user_id'] == user_id and order['amount'] == amount and order['currency'] == currency)


async def _read_order(connection: Any, payload: str) -> Any:
    return await connection.fetchrow('''SELECT o.*,p.telegram_payment_charge_id FROM donation_orders o
        LEFT JOIN donation_payments p ON p.order_id=o.id WHERE o.payload=$1''', payload)


async def create_order(connection: Any, user_id: int, amount: int) -> Any:
    """Create a single-user, single-use invoice with 192 bits of random payload."""
    _user(user_id)
    _amount(amount)
    payload = 'donation:v1:' + secrets.token_urlsafe(24)
    return await connection.fetchrow('''INSERT INTO donation_orders(payload,user_id,amount)
        VALUES($1,$2,$3) RETURNING *''', payload, user_id, amount)


async def validate_checkout(connection: Any, payload: str, user_id: int, currency: str,
                            total_amount: int, query_id: str) -> bool:
    """Atomically reserve an unexpired invoice for exactly one pre-checkout query."""
    try:
        _payload(payload)
        _user(user_id)
        _amount(total_amount, currency)
        _opaque(query_id)
    except ValueError:
        return False
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key('donation-query', query_id))
        order = await connection.fetchrow('''SELECT *,expires_at>clock_timestamp() AS unexpired
            FROM donation_orders WHERE payload=$1 FOR UPDATE''', payload)
        if not _matches(order, user_id, total_amount, currency) or order['status'] not in {'pending', 'checkout', 'expired'}:
            return False
        if not order['unexpired']:
            await connection.execute("UPDATE donation_orders SET status='expired' WHERE id=$1", order['id'])
            return False
        if order['checkout_query_id'] is not None:
            return order['checkout_query_id'] == query_id
        if await connection.fetchval('SELECT EXISTS(SELECT 1 FROM donation_orders WHERE checkout_query_id=$1)', query_id):
            return False
        await connection.execute("UPDATE donation_orders SET status='checkout',checkout_query_id=$2 WHERE id=$1", order['id'], query_id)
        return True


async def record_payment(connection: Any, *, payload: str, user_id: int, currency: str,
                         total_amount: int, charge_id: str) -> tuple[Any, bool]:
    """Record confirmed money once, even after expiry or a lost checkout update."""
    _payload(payload)
    _user(user_id)
    _amount(total_amount, currency)
    _opaque(charge_id)
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key('donation-charge', charge_id))
        order = await connection.fetchrow('SELECT * FROM donation_orders WHERE payload=$1 FOR UPDATE', payload)
        if not _matches(order, user_id, total_amount, currency):
            raise ValueError('Payment does not match its donation order')
        paid = await connection.fetchrow('''SELECT * FROM donation_payments
            WHERE order_id=$1 OR telegram_payment_charge_id=$2''', order['id'], charge_id)
        if paid:
            if (paid['order_id'] != order['id'] or paid['telegram_payment_charge_id'] != charge_id
                    or not _matches(paid, user_id, total_amount, currency)):
                raise ValueError('Payment identifier conflicts with a recorded donation')
            return await _read_order(connection, payload), False
        await connection.execute('''INSERT INTO donation_payments(
            telegram_payment_charge_id,order_id,payload,user_id,amount,currency)
            VALUES($1,$2,$3,$4,$5,$6)''', charge_id, order['id'], payload, user_id, total_amount, currency)
        await connection.execute("UPDATE donation_orders SET status='paid',paid_at=now() WHERE id=$1", order['id'])
        return await _read_order(connection, payload), True


async def list_user_donations(connection: Any, user_id: int, limit: int = 10) -> list[Any]:
    """Only this user's confirmed receipts; pending invoices are not donations."""
    _user(user_id)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid donation history limit')
    return await connection.fetch('''SELECT o.*,p.telegram_payment_charge_id FROM donation_orders o
        JOIN donation_payments p ON p.order_id=o.id WHERE o.user_id=$1
        ORDER BY p.paid_at DESC,o.id DESC LIMIT $2''', user_id, limit)


async def create_support_request(connection: Any, user_id: int, text: str) -> int:
    """Save a private support ticket; no payment details are posted to other chats."""
    _user(user_id)
    if not isinstance(text, str) or '\x00' in text or not 1 <= len(text.strip()) <= 4000:
        raise ValueError('Support request must contain 1 to 4000 characters')
    return await connection.fetchval('''INSERT INTO donation_support_requests(user_id,message)
        VALUES($1,$2) RETURNING id''', user_id, text.strip())


async def record_refund(connection: Any, *, payload: str, currency: str,
                        total_amount: int, charge_id: str) -> bool:
    """Append a confirmed full refund without altering the original receipt."""
    _payload(payload)
    _amount(total_amount, currency)
    _opaque(charge_id)
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock($1)', lock_key('donation-charge', charge_id))
        order = await connection.fetchrow('SELECT * FROM donation_orders WHERE payload=$1 FOR UPDATE', payload)
        if not order or order['amount'] != total_amount or order['currency'] != currency:
            raise ValueError('Refund does not match its donation order')
        paid = await connection.fetchrow('''SELECT * FROM donation_payments
            WHERE order_id=$1 AND telegram_payment_charge_id=$2''', order['id'], charge_id)
        if not paid:
            raise ValueError('Refund has no matching recorded payment')
        inserted = await connection.fetchval('''INSERT INTO donation_refunds(
            telegram_payment_charge_id,payload,currency,amount) VALUES($1,$2,$3,$4)
            ON CONFLICT(telegram_payment_charge_id) DO NOTHING RETURNING id''', charge_id, payload, currency, total_amount)
        if inserted is None:
            return False
        await connection.execute("UPDATE donation_orders SET status='refunded',refunded_at=now() WHERE id=$1", order['id'])
        return True
