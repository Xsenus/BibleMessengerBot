"""Database bootstrap entry point used by Docker Compose."""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from app.catalog.importer import run_import
from app.config import Settings
from app.db import apply_schema, normalize_asyncpg_dsn, wait_for_database, maintenance
from app.logging import configure_logging
from app.services.seed import seed_static_content
from app.services.verification import verify_database
from app.services.locks import lock_key

LOGGER = logging.getLogger(__name__)


async def bootstrap() -> dict[str, object]:
    settings = Settings.from_env(require_bot_token=False)
    await wait_for_database(settings)
    async with maintenance(settings):
        return await bootstrap_locked(settings)


async def bootstrap_locked(settings: Settings) -> dict[str,object]:
    """Run the entire upgrade/import/readiness operation under the maintenance guard."""
    await apply_schema(settings)

    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)",lock_key("static-seed",1))
            await seed_static_content(connection)
    finally:
        await connection.close()

    result: dict[str, object] = {"schema": "applied", "static_data": "seeded"}
    if settings.import_on_start and settings.bible_profile != "none":
        from app.catalog.multisource.cli import report_summary
        result["import"] = report_summary(await run_import(settings,maintenance_owned=True))
        if result['import']['status']=='failed':
            raise RuntimeError('No requested source editions could be acquired; see SOURCE_CACHE_DIR/reports/latest.json')
        if result['import']['status']=='partial':
            LOGGER.warning('Corpus acquisition is partial; usable editions will be checked before runtime starts')
    else:
        result["import"] = {"status": "skipped"}

    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        audit = await verify_database(connection,profile=settings.bible_profile,full=True)
    finally:
        await connection.close()
    result["database_audit"] = audit
    if audit['status']!='passed':
        LOGGER.error('Database audit failed: %s',json.dumps(audit,ensure_ascii=False,default=str))
        raise RuntimeError('Database integrity/readiness gate failed; inspect the audit report')

    LOGGER.info("Bootstrap result: %s", json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    configure_logging()
    print(json.dumps(asyncio.run(bootstrap()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
