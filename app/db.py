"""PostgreSQL pool and checksum-tracked, serialized additive migrations."""
from __future__ import annotations
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
from app.config import Settings
from app.services.locks import lock_key
if TYPE_CHECKING:
    import asyncpg

_pool: Any = None


def normalize_asyncpg_dsn(dsn: str) -> str:
    """Accept the SQLAlchemy prefix without otherwise rewriting credentials."""
    return dsn.replace('postgresql+asyncpg://','postgresql://',1)


async def create_pool(settings: Settings) -> Any:
    """Create one bounded pool per process. No DB module is needed for pure tests."""
    import asyncpg
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=normalize_asyncpg_dsn(settings.database_url),
            min_size=1,max_size=10,command_timeout=120,
            server_settings={'application_name':'BibleMessengerBot-1.3.0'})
    return _pool


async def close_pool() -> None:
    """Close all process connections on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire(settings: Settings) -> AsyncIterator[Any]:
    """Lease a pool connection for a bounded operation."""
    pool = await create_pool(settings)
    async with pool.acquire() as connection:
        yield connection


async def wait_for_database(settings: Settings, attempts: int = 60, delay: float = 2) -> None:
    """Bound startup retries; never log a credential-containing connection error."""
    import asyncpg
    for attempt in range(attempts):
        try:
            connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=5)
            await connection.close()
            return
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
    raise RuntimeError(f'PostgreSQL not ready after {attempts} attempts')


def migration_files(schema_path: Path | None = None) -> list[Path]:
    """The unchanged baseline schema plus sequential upgrade scripts."""
    base = schema_path or Path(__file__).resolve().parents[1] / 'sql' / 'schema.sql'
    return [base, *sorted((base.parent / 'migrations').glob('*.sql'))]


async def apply_schema(settings: Settings, schema_path: Path | None = None) -> None:
    """Apply each migration exactly once, rejecting changes to applied SQL."""
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
    try:
        await connection.execute('SELECT pg_advisory_lock($1)', lock_key('schema',1))
        await connection.execute('''CREATE TABLE IF NOT EXISTS schema_migrations(
            name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())''')
        for path in migration_files(schema_path):
            sql = path.read_text(encoding='utf-8')
            digest = hashlib.sha256(sql.encode()).hexdigest()
            old = await connection.fetchval('SELECT sha256 FROM schema_migrations WHERE name=$1',path.name)
            if old is not None:
                if old != digest:
                    raise RuntimeError(f'Applied migration was modified: {path.name}')
                continue
            # The baseline used explicit BEGIN/COMMIT; wrap it in a tracked transaction instead.
            body = sql.strip()
            if path.name == 'schema.sql':
                body = body.removeprefix('BEGIN;').removesuffix('COMMIT;')
            async with connection.transaction():
                await connection.execute(body)
                await connection.execute('INSERT INTO schema_migrations(name,sha256) VALUES($1,$2)',path.name,digest)
    finally:
        await connection.close()  # also releases the session migration lock


async def acquire_runtime_guard(connection: Any) -> None:
    """Refuse runtime startup while an exclusive schema/corpus maintenance operation runs."""
    if not await connection.fetchval('SELECT pg_try_advisory_lock_shared($1)',lock_key('maintenance',1)):
        raise RuntimeError('Database maintenance is active; runtime startup is blocked')


@asynccontextmanager
async def maintenance(settings: Settings) -> AsyncIterator[Any]:
    """Fail closed instead of mutating editions while bot/worker/admin are serving users."""
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
    try:
        if not await connection.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('maintenance',1)):
            raise RuntimeError('Stop bot, worker and admin before schema/corpus maintenance')
        yield connection
    finally:
        await connection.close()
