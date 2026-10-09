"""Recover missed notifications and lost API responses without new charge keys."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.payments import ledger
from app.payments.yookassa import MerchantSettings, PaymentError, YooKassa

LOGGER = logging.getLogger(__name__)


async def poll(connection, api, *, limit=10):
    orders = await connection.fetch('''SELECT * FROM native_payment_orders
        WHERE status IN ('creating','pending') AND created_at>now()-interval '7 days'
        AND (provider_payment_id IS NOT NULL OR created_at>now()-interval '23 hours')
        ORDER BY last_checked_at NULLS FIRST,created_at LIMIT $1''', limit)
    for order in orders:
        await connection.execute('UPDATE native_payment_orders SET last_checked_at=now() WHERE id=$1',order['id'])
        try:
            await ledger.checkout(connection, api, order)
        except PaymentError as error:
            LOGGER.warning('Native payment reconciliation deferred: %s', error.code)
    refunds = await connection.fetch('''SELECT * FROM native_payment_refunds
        WHERE status IN ('creating','pending') AND created_at>now()-interval '7 days'
        AND (provider_refund_id IS NOT NULL OR created_at>now()-interval '23 hours')
        ORDER BY last_checked_at NULLS FIRST,created_at LIMIT $1''', limit)
    for refund in refunds:
        await connection.execute('UPDATE native_payment_refunds SET last_checked_at=now() WHERE order_id=$1',refund['order_id'])
        try:
            await ledger.full_refund(connection, api, refund['order_id'])
        except PaymentError as error:
            LOGGER.warning('Native refund reconciliation deferred: %s', error.code)


async def loop(pool, stop):
    settings = MerchantSettings.from_env()
    if not settings.enabled:
        await stop.wait()
        return
    api = YooKassa(settings)
    try:
        while not stop.is_set():
            async with pool.acquire() as connection:
                await poll(connection, api)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60)
    finally:
        await api.close()
