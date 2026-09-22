"""Recover confirmed Stars payments/refunds if a polling update was interrupted."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.donations import record_payment, record_refund

LOGGER = logging.getLogger(__name__)


async def reconcile_page(bot: Any, pool: Any, offset: int = 0) -> dict[str, int]:
    page = await bot.get_star_transactions(offset=offset, limit=100, request_timeout=15)
    result = {'scanned': len(page.transactions), 'payments': 0, 'refunds': 0, 'rejected': 0}
    for transaction in page.transactions:
        partner = transaction.source or transaction.receiver
        if (not partner or getattr(partner, 'type', None) != 'user'
                or getattr(partner, 'transaction_type', None) != 'invoice_payment'
                or transaction.nanostar_amount or transaction.amount == 0):
            continue
        payload = getattr(partner, 'invoice_payload', None)
        async with pool.acquire() as connection:
            if not payload:
                # Telegram may omit invoice_payload on a refund transaction.
                payload = await connection.fetchval('SELECT payload FROM donation_payments WHERE telegram_payment_charge_id=$1', transaction.id)
            if not payload:
                continue
            # Other invoices issued by this bot are outside this ledger's scope.
            if not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM donation_orders WHERE payload=$1)', payload):
                continue
            try:
                _, newly = await record_payment(connection, payload=payload,
                    user_id=partner.user.id, currency='XTR', total_amount=abs(transaction.amount),
                    charge_id=transaction.id)
                result['payments'] += int(newly)
                if transaction.receiver is not None:
                    result['refunds'] += int(await record_refund(connection, payload=payload,
                        currency='XTR', total_amount=abs(transaction.amount), charge_id=transaction.id))
            except ValueError:
                # Never trust a matching payload alone over the stored user and amount.
                result['rejected'] += 1
                LOGGER.error('Stars reconciliation rejected mismatched transaction')
    return result


async def reconciliation_loop(bot: Any, pool: Any) -> None:
    """Check recent transactions and rotate through history, without sending messages."""
    key = f'donations:reconciliation-offset:{bot.id}'
    while True:
        try:
            await reconcile_page(bot, pool, 0)
            async with pool.acquire() as connection:
                saved = await connection.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
            offset = max(100, int(saved or 100))
            # Bound API work per cycle; the durable cursor eventually covers all history.
            for _ in range(5):
                result = await reconcile_page(bot, pool, offset)
                offset = offset + 100 if result['scanned'] == 100 else 100
                async with pool.acquire() as connection:
                    await connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
                        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', key, str(offset))
                if result['scanned'] < 100:
                    break
        except Exception as error:
            LOGGER.warning('Stars reconciliation deferred (%s)', type(error).__name__)
        await asyncio.sleep(300)
