"""Operational CLI for import, diagnostics, and database statistics."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import asyncpg

from app.bootstrap import bootstrap
from app.catalog.importer import run_import
from app.catalog.report import selection_summary
from app.catalog.selector import select_translations
from app.catalog.source import BibleNlpSource
from app.config import Settings
from app.db import apply_schema, normalize_asyncpg_dsn, wait_for_database, maintenance
from app.logging import configure_logging
from app.services.seed import seed_static_content
from app.services.verification import verify_database


async def catalog_preview(settings: Settings, refresh: bool) -> dict[str, Any]:
    from app.catalog.multisource.runner import acquire_corpus, process_lock
    from app.catalog.multisource.types import ImportOptions
    options=ImportOptions.from_env(profile=settings.bible_profile,max_editions=settings.max_editions_per_language)
    with process_lock(settings.source_cache_dir):
        return await acquire_corpus(settings.source_cache_dir,options,discover_only=True,refresh_catalog=refresh)


async def db_stats(settings: Settings) -> dict[str, Any]:
    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    try:
        counts = {}
        for table in (
            "languages",
            "translations",
            "books",
            "verses",
            "telegram_users",
            "telegram_chats",
            "subscriptions",
            "delivery_log",
            "import_runs",
        ):
            counts[table] = await connection.fetchval(f"SELECT COUNT(*) FROM {table}")
        editions = await connection.fetch(
            """
            SELECT l.code AS language, t.source_translation_id AS id, t.title,
                   t.coverage, t.book_count, t.verse_count, t.license_type,
                   t.imported_at, t.source_sha256
            FROM translations t
            JOIN languages l ON l.id=t.language_id
            WHERE t.is_active=true
            ORDER BY l.code, t.title
            """
        )
        counts["editions"] = [dict(row) for row in editions]
        return counts
    finally:
        await connection.close()


async def seed_only(settings: Settings) -> dict[str, str]:
    await wait_for_database(settings)
    async with maintenance(settings) as connection:
        await apply_schema(settings)
        async with connection.transaction():
            await seed_static_content(connection)
    return {"schema": "applied", "static_data": "seeded"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BibleMessengerBot operations")
    result.add_argument("command", choices=("bootstrap", "import", "preview", "stats", "seed", "audit"))
    result.add_argument("--profile", choices=("core", "extended", "all-open", "none"))
    result.add_argument("--max-editions", type=int)
    result.add_argument("--refresh", action="store_true")
    result.add_argument("--output", type=Path)
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings.from_env(require_bot_token=False)
    updates: dict[str, Any] = {}
    if args.profile:
        updates["bible_profile"] = args.profile
    if args.max_editions:
        updates["max_editions_per_language"] = args.max_editions
    if updates:
        settings = replace(settings, **updates)

    if args.command == "bootstrap":
        return await bootstrap(settings)
    if args.command == "import":
        await seed_only(settings)
        return await run_import(
            settings,
            refresh_catalog=args.refresh,
            refresh_translations=args.refresh,
        )
    if args.command == "preview":
        return await catalog_preview(settings, args.refresh)
    if args.command == "audit":
        connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
        try:
            return await verify_database(connection,profile=settings.bible_profile,full=True)
        finally:
            await connection.close()
    if args.command == "stats":
        return await db_stats(settings)
    if args.command == "seed":
        return await seed_only(settings)
    raise AssertionError(args.command)


def main() -> None:
    import sys
    configure_logging(stream=sys.stderr)
    args = parser().parse_args()
    result = asyncio.run(run(args))
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if result.get("status") == "failed":
        raise SystemExit(1)
    if result.get("status") == "partial":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
