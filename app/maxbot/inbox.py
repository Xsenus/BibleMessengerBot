"""Bounded event consumers with FIFO per external chat and no blind crash replay."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)
CONCURRENCY = 4


async def due_events(connection, busy=()):
    return await connection.fetch('''SELECT id,chat_key FROM (
        SELECT DISTINCT ON (chat_key) id,chat_key FROM max_inbox i
        WHERE status='pending' AND NOT(chat_key=ANY($1::text[]))
        AND NOT EXISTS(SELECT 1 FROM max_inbox active WHERE active.chat_key=i.chat_key AND active.status='processing')
        ORDER BY chat_key,id) q ORDER BY id LIMIT $2''',list(busy),CONCURRENCY-len(busy))


async def recover(connection):
    """Only the singleton owning consumer may recover interrupted event processing."""
    await connection.execute("UPDATE max_inbox SET status='uncertain',error_code='consumer_interrupted',updated_at=now() WHERE status='processing'")


async def process(pool, dispatcher, identifier):
    async with pool.acquire() as connection:
        row = await connection.fetchrow("""UPDATE max_inbox SET status='processing',attempts=attempts+1,updated_at=now()
            WHERE id=$1 AND status='pending' RETURNING payload,event_key""",identifier)
    if not row:
        return 'stale'
    try:
        event = json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        await dispatcher.dispatch(event,row['event_key'])
    except UserError as error:
        status, code = 'failed', error.key
    except Exception as error:
        # Effects may have committed before an unexpected exception. Do not
        # execute commands a second time or repeat domain writes automatically.
        status, code = 'uncertain', type(error).__name__
        LOGGER.error('MAX event %s requires review (%s)',identifier,code)
    else:
        status, code = 'done', None
    async with pool.acquire() as connection:
        await connection.execute("UPDATE max_inbox SET status=$2,error_code=$3,updated_at=now() WHERE id=$1 AND status='processing'",identifier,status,code)
    return status


async def consumer_loop(pool, dispatcher, stop):
    active = {}
    try:
        while not stop.is_set():
            for key, task in tuple(active.items()):
                if task.done():
                    task.result()
                    del active[key]
            if len(active)<CONCURRENCY:
                async with pool.acquire() as connection:
                    pending = await due_events(connection,active)
                for row in pending:
                    active[row['chat_key']] = asyncio.create_task(process(pool,dispatcher,row['id']))
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(),timeout=0.25)
        if active:
            await asyncio.wait_for(asyncio.gather(*active.values()),timeout=35)
    finally:
        for task in active.values():
            task.cancel()
        await asyncio.gather(*active.values(),return_exceptions=True)
