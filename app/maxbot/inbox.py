"""Потребители входящих событий MAX: в одном чате строго по очереди, без слепого повтора после сбоя."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)
CONCURRENCY = 4


async def due_events(connection, busy=()):
    """Выбрать события к обработке: по одному старшему событию на чат, пропуская чаты, где уже идёт обработка, не больше свободных слотов."""
    return await connection.fetch('''SELECT id,chat_key FROM (
        SELECT DISTINCT ON (chat_key) id,chat_key FROM max_inbox i
        WHERE status='pending' AND NOT(chat_key=ANY($1::text[]))
        AND NOT EXISTS(SELECT 1 FROM max_inbox active WHERE active.chat_key=i.chat_key AND active.status='processing')
        ORDER BY chat_key,id) q ORDER BY id LIMIT $2''',list(busy),CONCURRENCY-len(busy))


async def recover(connection):
    """После старта единственного потребителя пометить прерванные (`processing`) события как неопределённые — повторно команды не выполняются."""
    await connection.execute("UPDATE max_inbox SET status='uncertain',error_code='consumer_interrupted',updated_at=now() WHERE status='processing'")


async def process(pool, dispatcher, identifier):
    """Обработать одно событие: перевести в `processing`, выполнить диспетчер и записать итог: `done`, `failed` (ошибка пользователя) или `uncertain` (неожиданный сбой — эффекты могли уже зафиксироваться, поэтому автоповтора нет)."""
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
    """Основной цикл потребителя: до 4 чатов параллельно, при остановке ждёт завершения текущих событий до 35 секунд."""
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
