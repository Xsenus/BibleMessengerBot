"""Database bootstrap entry point used by Docker Compose."""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from app.catalog.importer import run_import
from app.config import Settings
from app.db import apply_schema, normalize_asyncpg_dsn, wait_for_database
from app.logging import configure_logging
from app.services.seed import seed_static_content

LOGGER = logging.getLogger(__name__)


async def bootstrap() -> dict[str, object]:
    settings = Settings.from_env(require_bot_token=False)
    await wait_for_database(settings)
    await apply_schema(settings)

    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        await seed_static_content(connection)
    finally:
        await connection.close()

    result: dict[str, object] = {"schema": "applied", "static_data": "seeded"}
    if settings.import_on_start and settings.bible_profile != "none":
        result["import"] = await run_import(settings)
    else:
        result["import"] = {"status": "skipped"}

    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        usable_editions = await connection.fetchval(
            "SELECT COUNT(*) FROM translations WHERE is_active=true AND verse_count>0"
        )
    finally:
        await connection.close()
    result["usable_editions"] = usable_editions
    if settings.bible_profile != "none" and not usable_editions:
        raise RuntimeError("Bootstrap completed without any usable Bible edition")

    LOGGER.info("Bootstrap result: %s", json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    configure_logging()
    print(json.dumps(asyncio.run(bootstrap()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
