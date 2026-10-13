"""Стабильные ключи advisory-блокировок PostgreSQL для импорта, отправки и настроек."""
from __future__ import annotations
import hashlib
from contextlib import asynccontextmanager
from typing import Any
from collections.abc import AsyncIterator


def lock_key(namespace: str, identifier: object) -> int:
    """Детерминированный знаковый bigint-ключ из пары «пространство имён : идентификатор» (не зависит от случайного хеширования Python)."""
    return int.from_bytes(hashlib.sha256(f'{namespace}:{identifier}'.encode()).digest()[:8],
                          'big', signed=True)


@asynccontextmanager
async def chat_lock(connection: Any, chat_id: int) -> AsyncIterator[None]:
    """Контекстный менеджер: блокировка чата, сериализующая изменения и отправку одной части, без долгой транзакции в БД."""
    key = lock_key('chat', chat_id)
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        yield
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)
