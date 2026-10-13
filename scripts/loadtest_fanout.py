"""Замер скорости рассылки: сколько чатов в секунду ставится в очередь доставки.

Скрипт сам создаёт временную базу (рядом с указанной в DATABASE_URL), применяет схему и удаляет базу по окончании — ваши данные
не затрагиваются.

    DATABASE_URL=postgresql://... python -m scripts.loadtest_fanout 5000
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

from app.config import Settings
from app.db import apply_schema
from app.services import fanout

BASE = 9_000_000_000_000


async def main(count: int) -> None:
    original = os.environ['DATABASE_URL']
    name = 'biblebot_load_' + uuid4().hex[:12]
    dsn = urlunsplit(urlsplit(original)._replace(path='/' + name))
    admin = await asyncpg.connect(original)
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    connection = None
    try:
        await apply_schema(replace(Settings.from_env(require_bot_token=False), database_url=dsn, bible_profile='none'))
        connection = await asyncpg.connect(dsn)
        await measure(connection, count)
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def measure(connection: asyncpg.Connection, count: int) -> None:
    """Создать `count` чатов трёх платформ и замерить пакетную и поштучную постановку в очередь."""
    await connection.execute('''INSERT INTO telegram_chats(telegram_chat_id,chat_type,title,timezone,platform)
        SELECT $1::bigint+g,'channel','load '||g,'UTC',(ARRAY['telegram','max','vk'])[1+g%3] FROM generate_series(1,$2) g''', BASE, count)
    text = 'Нагрузочный тест ' * 20
    started = time.perf_counter()
    result = await fanout.enqueue(connection, key='loadtest:batch', text=text, mode='announcement', audience={'chat_types': ['channel']})
    batch = time.perf_counter() - started
    sample = (await fanout.recipients(connection, {'chat_types': ['channel']}))[:300]
    started = time.perf_counter()
    for target in sample:                                       # прежний путь «по одному чату» — для сравнения
        await fanout._enqueue_one(connection, target, text, 'loadtest:one', 'announcement', {}, None, 3900)  # noqa: SLF001
    single = (time.perf_counter() - started) / max(1, len(sample))
    print(f"пакетно: {result['queued']} чатов за {batch:.2f} с ({result['queued'] / batch:.0f} чатов/с)")
    print(f'по одному (прежний путь): {1 / single:.0f} чатов/с  →  ускорение примерно в {(result["queued"] / batch) * single:.0f} раз')


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5000))
