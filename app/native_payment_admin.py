"""Local operator-only native receipts, support tickets and full refund command."""
from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool
from app.logging import configure_logging
from app.payments import ledger
from app.payments.reconcile import poll
from app.payments.yookassa import MerchantSettings, YooKassa


async def run(args):
    pool = await create_pool(Settings.from_env(require_bot_token=False))
    api = None
    try:
        async with pool.acquire() as connection:
            await acquire_runtime_guard(connection)
            if args.action == 'history':
                rows = await connection.fetch('''SELECT id,user_id,amount_minor,method,status,provider_test,
                    created_at,confirmed_at FROM native_payment_orders ORDER BY created_at DESC LIMIT 50''')
                print(json.dumps([dict(row) for row in rows], default=str, ensure_ascii=False))
            elif args.action == 'support':
                rows = await connection.fetch('SELECT * FROM native_payment_support ORDER BY created_at DESC LIMIT 50')
                print(json.dumps([dict(row) for row in rows], default=str, ensure_ascii=False))
            else:
                api = YooKassa(MerchantSettings.from_env())
                if args.action == 'refund':
                    result = await ledger.full_refund(connection, api, UUID(args.order_id))
                    print(json.dumps({'order_id': str(result['order_id']), 'status': result['status']}))
                elif args.action == 'verify':
                    result = await ledger.record_payment(connection,UUID(args.order_id),
                                                          await api.payment(args.payment_id),api.settings.shop_id)
                    print(json.dumps({'order_id':str(result['id']),'status':result['status']}))
                else:
                    await poll(connection, api)
                    print('Reconciliation completed')
    finally:
        if api:
            await api.close()
        await close_pool()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('history')
    sub.add_parser('support')
    sub.add_parser('reconcile')
    refund = sub.add_parser('refund')
    refund.add_argument('order_id', type=str)
    verify=sub.add_parser('verify',help='Bind an orphaned merchant payment only after authenticated GET and exact metadata/amount validation')
    verify.add_argument('order_id',type=str)
    verify.add_argument('payment_id',type=str)
    args = parser.parse_args()
    if args.action in {'refund','verify'}:
        UUID(args.order_id)
    configure_logging()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
