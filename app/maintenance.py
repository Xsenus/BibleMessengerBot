"""Bounded-growth housekeeping: prune old operational rows in small batches.

Nothing here touches Bible text, user settings, subscriptions, payments, image budget
history or anything that is still outstanding. Defaults can be changed through
RETENTION_* environment variables; `--dry-run` only counts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass

from app.config import Settings
from app.db import close_pool, create_pool, wait_for_database

BATCH = 1000


@dataclass(frozen=True)
class Rule:
    name: str
    env: str
    days: int
    table: str
    where: str  # may use $1 as the cutoff timestamp


RULES = (
    Rule('delivery_log', 'RETENTION_DELIVERY_DAYS', 365, 'delivery_log',
         """status IN ('sent','skipped') AND updated_at<$1
            AND NOT EXISTS(SELECT 1 FROM prayer_occurrences p WHERE p.delivery_id=delivery_log.id)"""),
    Rule('max_inbox', 'RETENTION_MAX_INBOX_DAYS', 14, 'max_inbox', "status='done' AND updated_at<$1"),
    Rule('operator_events', 'RETENTION_EVENTS_DAYS', 180, 'operator_events', 'created_at<$1'),
    Rule('usage_events', 'RETENTION_USAGE_DAYS', 400, 'usage_events', 'day<($1)::date'),
)


def days_for(rule: Rule) -> int:
    try:
        return max(7, int(os.getenv(rule.env, rule.days)))
    except ValueError:
        return rule.days


async def prune(connection, rule: Rule, *, dry_run: bool = False) -> int:
    """Delete (or count) eligible rows; each batch is its own short transaction."""
    cutoff = await connection.fetchval("SELECT now()-make_interval(days=>$1)", days_for(rule))
    if dry_run:
        return await connection.fetchval(f'SELECT count(*) FROM {rule.table} WHERE {rule.where}', cutoff)
    total = 0
    while True:
        status = await connection.execute(
            f'DELETE FROM {rule.table} WHERE ctid IN (SELECT ctid FROM {rule.table} WHERE {rule.where} LIMIT {BATCH})', cutoff)
        removed = int(status.rsplit(' ', 1)[-1])
        total += removed
        if removed < BATCH:
            return total
        await asyncio.sleep(0.05)  # leave room for live traffic between batches


async def run(dry_run: bool = False) -> dict[str, int]:
    settings = Settings.from_env(require_bot_token=False)
    await wait_for_database(settings)
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as connection:
            result = {rule.name: await prune(connection, rule, dry_run=dry_run) for rule in RULES}
            if not dry_run:
                await connection.execute('ANALYZE delivery_log')
        return result
    finally:
        await close_pool()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    from app.logging import configure_logging
    configure_logging()
    print(json.dumps(asyncio.run(run(args.dry_run)), ensure_ascii=False))
