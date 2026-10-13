"""Надёжная входящая очередь событий платформы: в одном чате строго по порядку, без слепого повтора после сбоя."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from app.platforms.spec import PlatformSpec
from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)
CONCURRENCY = 4


class InboxQueue:
    """Очередь событий одной платформы (таблица `spec.inbox_table`, имя берётся только из описания платформы)."""

    def __init__(self, spec: PlatformSpec) -> None:
        self.spec = spec
        self.table = spec.inbox_table

    async def due_events(self, connection: Any, busy: Any = ()) -> list[Any]:
        """События к обработке: по одному старшему на чат, пропуская чаты, где идёт обработка, не больше свободных слотов."""
        return await connection.fetch(f'''SELECT id,chat_key FROM (
            SELECT DISTINCT ON (chat_key) id,chat_key FROM {self.table} i
            WHERE status='pending' AND NOT(chat_key=ANY($1::text[]))
            AND NOT EXISTS(SELECT 1 FROM {self.table} active WHERE active.chat_key=i.chat_key AND active.status='processing')
            ORDER BY chat_key,id) q ORDER BY id LIMIT $2''', list(busy), CONCURRENCY - len(busy))  # noqa: S608

    async def recover(self, connection: Any) -> None:
        """После старта единственного потребителя прерванные (`processing`) события помечаются неопределёнными — команды не выполняются повторно."""
        await connection.execute(f"UPDATE {self.table} SET status='uncertain',error_code='consumer_interrupted',updated_at=now() WHERE status='processing'")  # noqa: S608

    async def process(self, pool: Any, dispatcher: Any, identifier: int) -> str:
        """Обработать одно событие: `processing` → диспетчер → `done`, `failed` (ошибка пользователя) или `uncertain` (неожиданный сбой:
        эффекты могли уже зафиксироваться, поэтому автоповтора нет)."""
        async with pool.acquire() as connection:
            row = await connection.fetchrow(f"""UPDATE {self.table} SET status='processing',attempts=attempts+1,updated_at=now()
                WHERE id=$1 AND status='pending' RETURNING payload,event_key""", identifier)  # noqa: S608
        if not row:
            return 'stale'
        code: str | None
        try:
            event = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
            await dispatcher.dispatch(event, row['event_key'])
        except UserError as error:
            status, code = 'failed', error.key
        except Exception as error:
            status, code = 'uncertain', type(error).__name__
            LOGGER.error('%s event %s requires review (%s)', self.spec.title, identifier, code)
        else:
            status, code = 'done', None
        async with pool.acquire() as connection:
            await connection.execute(f"UPDATE {self.table} SET status=$2,error_code=$3,updated_at=now() WHERE id=$1 AND status='processing'",  # noqa: S608
                                     identifier, status, code)
        return status

    async def consumer_loop(self, pool: Any, dispatcher: Any, stop: asyncio.Event) -> None:
        """Основной цикл потребителя: до 4 чатов параллельно; при остановке ждёт завершения текущих событий до 35 секунд."""
        active: dict[str, asyncio.Task] = {}
        try:
            while not stop.is_set():
                for key, task in tuple(active.items()):
                    if task.done():
                        task.result()
                        del active[key]
                if len(active) < CONCURRENCY:
                    async with pool.acquire() as connection:
                        pending = await self.due_events(connection, active)
                    for row in pending:
                        active[row['chat_key']] = asyncio.create_task(self.process(pool, dispatcher, row['id']))
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
            if active:
                await asyncio.wait_for(asyncio.gather(*active.values()), timeout=35)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
