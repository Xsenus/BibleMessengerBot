"""Server-only Stars support/refunds. Access is controlled by host credentials."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.config import Settings
from app.db import normalize_asyncpg_dsn
from app.services.donation_reconciliation import reconcile_page
from app.services.donations import record_refund
from app.services.locks import lock_key


async def refund_order(connection: Any, bot: Any, order_id: int, *, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError('Inspect payments first; refund requires --confirm')
    key = lock_key('donation-admin-refund', order_id)
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        order = await connection.fetchrow('''SELECT o.*,p.telegram_payment_charge_id
            FROM donation_orders o LEFT JOIN donation_payments p ON p.order_id=o.id WHERE o.id=$1''', order_id)
        if not order or not order['telegram_payment_charge_id']:
            raise ValueError('No confirmed payment for this order')
        if order['status'] == 'refunded':
            return {'order_id': order_id, 'status': 'already_refunded'}
        try:
            accepted = await bot.refund_star_payment(user_id=order['user_id'],
                telegram_payment_charge_id=order['telegram_payment_charge_id'], request_timeout=15)
        except TelegramBadRequest as error:
            # Repeating a refund after a lost response uses the SAME charge ID.
            if 'CHARGE_ALREADY_REFUNDED' not in error.message.upper():
                raise
            accepted = True
        if accepted is not True:
            raise RuntimeError('Telegram did not confirm the refund; reconcile before retrying')
        await record_refund(connection, payload=order['payload'], currency=order['currency'],
            total_amount=order['amount'], charge_id=order['telegram_payment_charge_id'])
        return {'order_id': order_id, 'status': 'refunded', 'stars': order['amount']}
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)


async def reply_support(connection: Any, bot: Any, ticket_id: int, message: str) -> dict:
    message = message.strip()
    if not 1 <= len(message) <= 3500 or '\x00' in message:
        raise ValueError('Reply must contain 1 to 3500 characters')
    key = lock_key('donation-support-reply', ticket_id)
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        ticket = await connection.fetchrow('SELECT * FROM donation_support_requests WHERE id=$1', ticket_id)
        if not ticket:
            raise ValueError('Support request not found')
        if ticket['status'] == 'closed':
            raise ValueError('Support request already closed')
        # A CLI invocation is an explicit operator send; never retry ambiguously.
        await bot.send_message(chat_id=ticket['user_id'], text=f'#{ticket_id}\n\n{message}',
                               parse_mode=None, request_timeout=15)
        await connection.execute("""UPDATE donation_support_requests SET status='closed',
            reply_text=$2,replied_at=now(),updated_at=now() WHERE id=$1""", ticket_id, message)
        return {'ticket_id': ticket_id, 'status': 'answered'}
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description='Private donation ledger and support operations')
    sub = root.add_subparsers(dest='command', required=True)
    sub.add_parser('payments', help='Last 100 confirmed payments')
    sub.add_parser('support', help='Open support requests')
    sub.add_parser('balance', help='Read the live Telegram Stars balance')
    reconcile = sub.add_parser('reconcile', help='Recover ledger from authoritative Telegram transactions')
    reconcile.add_argument('--pages', type=int, default=20)
    refund = sub.add_parser('refund', help='Refund a confirmed donation in full')
    refund.add_argument('order_id', type=int)
    refund.add_argument('--confirm', action='store_true')
    close = sub.add_parser('support-close', help='Close a resolved request without sending a message')
    close.add_argument('ticket_id', type=int)
    reply = sub.add_parser('support-reply', help='Send one private reply and close the request')
    reply.add_argument('ticket_id', type=int)
    reply.add_argument('--message-file', type=Path, required=True)
    return root


async def run(args: argparse.Namespace) -> Any:
    settings = Settings.from_env(require_bot_token=False)
    pool = await asyncpg.create_pool(normalize_asyncpg_dsn(settings.database_url),
                                    min_size=1, max_size=2, command_timeout=20)
    bot = None
    try:
        if args.command in {'refund', 'support-reply', 'balance', 'reconcile'}:
            bot = Bot(settings.bot_token)
        if args.command == 'balance':
            return (await bot.get_my_star_balance(request_timeout=15)).model_dump()
        if args.command == 'reconcile':
            if not 1 <= args.pages <= 1000:
                raise ValueError('Choose 1 to 1000 pages')
            totals = {'scanned': 0, 'payments': 0, 'refunds': 0, 'rejected': 0}
            for page in range(args.pages):
                result = await reconcile_page(bot, pool, page * 100)
                for key, value in result.items():
                    totals[key] += value
                if result['scanned'] < 100:
                    break
            return totals
        async with pool.acquire() as connection:
            if args.command == 'payments':
                return [dict(row) for row in await connection.fetch('''SELECT o.id,o.user_id,o.amount,
                    o.currency,o.status,o.paid_at,o.refunded_at,p.telegram_payment_charge_id FROM donation_orders o
                    JOIN donation_payments p ON p.order_id=o.id ORDER BY o.paid_at DESC LIMIT 100''')]
            if args.command == 'support':
                return [dict(row) for row in await connection.fetch("SELECT * FROM donation_support_requests WHERE status='open' ORDER BY created_at LIMIT 100")]
            if args.command == 'support-close':
                result = await connection.execute("UPDATE donation_support_requests SET status='closed',updated_at=now() WHERE id=$1 AND status='open'", args.ticket_id)
                return {'ticket_id': args.ticket_id, 'changed': result == 'UPDATE 1'}
            if args.command == 'support-reply':
                return await reply_support(connection, bot, args.ticket_id, args.message_file.read_text(encoding='utf-8'))
            if args.command == 'refund':
                return await refund_order(connection, bot, args.order_id, confirmed=args.confirm)
        raise ValueError('Unknown command')
    finally:
        if bot:
            await bot.session.close()
        await pool.close()


def main() -> None:
    try:
        result = asyncio.run(run(parser().parse_args()))
    except Exception as error:
        # API/network exceptions can include sensitive URLs; don't print bodies.
        print(json.dumps({'error': type(error).__name__, 'hint':
            'Check arguments and ledger. A failed network call is uncertain; reconcile before retrying a refund. Do not blindly resend support replies.'}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
