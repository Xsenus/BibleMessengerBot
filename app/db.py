"""PostgreSQL pool and schema bootstrap helpers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg

from app.config import Settings

_pool: asyncpg.Pool | None = None


def normalize_asyncpg_dsn(dsn: str) -> str:
    """Accept SQLAlchemy-style PostgreSQL DSNs while using asyncpg directly."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=normalize_asyncpg_dsn(settings.database_url),
            min_size=1,
            max_size=10,
            command_timeout=120,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire(settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    pool = await create_pool(settings)
    async with pool.acquire() as connection:
        yield connection


async def wait_for_database(settings: Settings, attempts: int = 60, delay: float = 2.0) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=5)
            await connection.close()
            return
        except Exception as exc:  # noqa: BLE001 - startup diagnostics need the final DB error
            last_error = exc
            await asyncio.sleep(delay)
    raise RuntimeError(f"PostgreSQL is not ready after {attempts} attempts: {last_error}")


async def apply_schema(settings: Settings, schema_path: Path | None = None) -> None:
    path = schema_path or Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    sql = path.read_text(encoding="utf-8")
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()
