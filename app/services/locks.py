"""Stable PostgreSQL advisory lock keys shared by importer, sender and settings."""
from __future__ import annotations
import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


def lock_key(namespace: str, identifier: object) -> int:
    """Produce a signed bigint, independent of Python hash randomization."""
    return int.from_bytes(hashlib.sha256(f'{namespace}:{identifier}'.encode()).digest()[:8],
                          'big', signed=True)


@asynccontextmanager
async def chat_lock(connection: Any, chat_id: int) -> AsyncIterator[None]:
    """Serialize mutations and one outbound chunk without a long DB transaction."""
    key = lock_key('chat', chat_id)
    await connection.execute('SELECT pg_advisory_lock($1)', key)
    try:
        yield
    finally:
        await connection.execute('SELECT pg_advisory_unlock($1)', key)
