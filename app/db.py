"""Пул соединений PostgreSQL и применение миграций схемы (каждая миграция выполняется один раз, с контролем контрольной суммы)."""
from __future__ import annotations
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator
from app.config import Settings
from app.services.locks import lock_key
if TYPE_CHECKING:
    pass

_pool: Any = None


def normalize_asyncpg_dsn(dsn: str) -> str:
    """Привести строку подключения к виду, который понимает asyncpg: заменить префикс SQLAlchemy `postgresql+asyncpg://` на `postgresql://`, остальное (логин, пароль) не трогая."""
    return dsn.replace('postgresql+asyncpg://','postgresql://',1)


async def create_pool(settings: Settings) -> Any:
    """Вернуть единственный на процесс пул соединений (создаётся при первом вызове, до 10 соединений, тайм-аут команды 120 с). Модуль asyncpg импортируется лениво, чтобы чистые тесты обходились без БД."""
    import asyncpg
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=normalize_asyncpg_dsn(settings.database_url),
            min_size=1,max_size=10,command_timeout=120,
            server_settings={'application_name':'BibleMessengerBot-1.20.0'})
    return _pool


async def close_pool() -> None:
    """Закрыть пул соединений процесса при остановке службы; повторный вызов безопасен."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire(settings: Settings) -> AsyncIterator[Any]:
    """Контекстный менеджер: взять соединение из пула на время короткой операции и вернуть его обратно."""
    pool = await create_pool(settings)
    async with pool.acquire() as connection:
        yield connection


async def wait_for_database(settings: Settings, attempts: int = 60, delay: float = 2) -> None:
    """Дождаться готовности PostgreSQL: до `attempts` попыток с паузой `delay` секунд. Текст ошибки подключения не выводится, чтобы пароль не попал в журнал."""
    import asyncpg
    for attempt in range(attempts):
        try:
            connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=5)
            await connection.close()
            return
        except (TimeoutError, OSError, asyncpg.PostgresError):
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
    raise RuntimeError(f'PostgreSQL not ready after {attempts} attempts')


def migration_files(schema_path: Path | None = None) -> list[Path]:
    """Список SQL-файлов для применения по порядку: базовая схема `sql/schema.sql`, затем миграции из `sql/migrations/` по имени файла."""
    base = schema_path or Path(__file__).resolve().parents[1] / 'sql' / 'schema.sql'
    return [base, *sorted((base.parent / 'migrations').glob('*.sql'))]


async def migration_status(settings: Settings, schema_path: Path | None = None) -> dict[str, Any]:
    """Предварительная проверка миграций без изменений: какие уже применены, какие ожидают, какие применённые файлы изменены."""
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
    try:
        exists = await connection.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")
        known = {r['name']: r['sha256'] for r in await connection.fetch('SELECT name,sha256 FROM schema_migrations')} if exists else {}
    finally:
        await connection.close()
    applied, pending, modified = [], [], []
    for path in migration_files(schema_path):
        digest = hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest()
        if path.name not in known:
            pending.append(path.name)
        elif known[path.name] != digest:
            modified.append(path.name)
        else:
            applied.append(path.name)
    return {'status': 'failed' if modified else 'pending' if pending else 'ok', 'applied': len(applied), 'pending': pending, 'modified': modified}


async def apply_schema(settings: Settings, schema_path: Path | None = None) -> None:
    """Применить все ещё не применённые миграции под общей блокировкой. Каждая миграция выполняется в своей транзакции, её SHA-256 записывается в `schema_migrations`; если уже применённый файл изменён — выбрасывается ошибка."""
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
    """Взять разделяемую блокировку обслуживания: рабочие службы запускаются только если сейчас не идёт эксклюзивная миграция или импорт корпуса."""
    if not await connection.fetchval('SELECT pg_try_advisory_lock_shared($1)',lock_key('maintenance',1)):
        raise RuntimeError('Database maintenance is active; runtime startup is blocked')


@asynccontextmanager
async def maintenance(settings: Settings) -> AsyncIterator[Any]:
    """Контекстный менеджер эксклюзивного обслуживания (миграции, импорт изданий). Если бот, воркер или админка уже работают, сразу отказывает, чтобы не менять данные под пользователями."""
    import asyncpg
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
    try:
        if not await connection.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('maintenance',1)):
            raise RuntimeError('Stop bot, worker and admin before schema/corpus maintenance')
        yield connection
    finally:
        await connection.close()
