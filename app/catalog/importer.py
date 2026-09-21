"""Transactional, idempotent import of selected Bible editions."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import asyncpg

from app.catalog.models import DownloadedTranslation, TranslationMeta, VerseReference
from app.catalog.references import pair_verses, parse_reference_lines
from app.catalog.report import selection_summary
from app.catalog.selector import SelectionItem, SelectionResult, select_translations
from app.catalog.source import BibleNlpSource
from app.config import Settings
from app.db import normalize_asyncpg_dsn

LOGGER = logging.getLogger(__name__)


async def seed_books(connection: asyncpg.Connection, books_path: Path | None = None) -> None:
    path = books_path or Path(__file__).resolve().parents[2] / "data" / "books.json"
    books = json.loads(path.read_text(encoding="utf-8"))
    for item in books:
        await connection.execute(
            """
            INSERT INTO books(code, canonical_order, testament, default_name)
            VALUES($1, $2, $3, $4)
            ON CONFLICT (code) DO UPDATE SET
                canonical_order = EXCLUDED.canonical_order,
                testament = EXCLUDED.testament,
                default_name = EXCLUDED.default_name
            """,
            item["code"],
            item["canonical_order"],
            item["testament"],
            item["default_name"],
        )
        for language_code, name in item.get("names", {}).items():
            await connection.execute(
                """
                INSERT INTO book_names(book_code, language_code, name, short_name)
                VALUES($1, $2, $3, $3)
                ON CONFLICT (book_code, language_code) DO UPDATE
                SET name = EXCLUDED.name, short_name = EXCLUDED.short_name
                """,
                item["code"],
                language_code,
                name,
            )


async def ensure_reference_books(
    connection: asyncpg.Connection,
    references: Iterable[VerseReference],
) -> None:
    ordered_codes = list(dict.fromkeys(reference.book_code for reference in references))
    existing_rows = await connection.fetch("SELECT code FROM books WHERE code = ANY($1::text[])", ordered_codes)
    existing = {row["code"] for row in existing_rows}
    missing = [code for code in ordered_codes if code not in existing]
    if not missing:
        return
    maximum = await connection.fetchval("SELECT COALESCE(MAX(canonical_order), 0) FROM books")
    for offset, code in enumerate(missing, start=1):
        await connection.execute(
            """
            INSERT INTO books(code, canonical_order, testament, default_name)
            VALUES($1, $2, 'DC', $1)
            ON CONFLICT (code) DO NOTHING
            """,
            code,
            int(maximum) + offset,
        )


def _metadata_json(metadata: TranslationMeta) -> str:
    return json.dumps(
        {
            "language_name": metadata.language_name,
            "language_name_english": metadata.language_name_english,
            "ot_chapters": metadata.ot_chapters,
            "ot_verses": metadata.ot_verses,
            "nt_chapters": metadata.nt_chapters,
            "nt_verses": metadata.nt_verses,
            "dc_chapters": metadata.dc_chapters,
            "dc_verses": metadata.dc_verses,
            "upstream": metadata.extra,
        },
        ensure_ascii=False,
    )


async def _is_up_to_date(
    connection: asyncpg.Connection,
    downloaded: DownloadedTranslation,
) -> bool:
    row = await connection.fetchrow(
        """
        SELECT source_sha256, verse_count
        FROM translations
        WHERE source_name = 'BibleNLP/eBible' AND source_translation_id = $1
        """,
        downloaded.metadata.translation_id,
    )
    return bool(
        row
        and row["source_sha256"] == downloaded.sha256
        and int(row["verse_count"] or 0) > 0
    )


async def import_translation(
    connection: asyncpg.Connection,
    downloaded: DownloadedTranslation,
    references: list[VerseReference],
    *,
    batch_size: int = 2000,
) -> dict[str, Any]:
    metadata = downloaded.metadata
    text_lines = downloaded.path.read_text(encoding="utf-8-sig").splitlines()
    records = pair_verses(references, text_lines)
    if not records:
        raise ValueError(f"Translation {metadata.translation_id} contains no nonblank verses")

    actual_books = len({record[0] for record in records})
    actual_nonempty = sum(1 for record in records if record[3] and not record[4])
    license_info = metadata.license

    async with connection.transaction():
        await ensure_reference_books(connection, references)
        language_id = await connection.fetchval(
            """
            INSERT INTO languages(code, name, english_name, script, text_direction)
            VALUES($1, $2, $3, NULLIF($4, ''), $5)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                english_name = EXCLUDED.english_name,
                script = COALESCE(EXCLUDED.script, languages.script),
                text_direction = EXCLUDED.text_direction
            RETURNING id
            """,
            metadata.language_code,
            metadata.language_name,
            metadata.language_name_english,
            metadata.script,
            metadata.text_direction if metadata.text_direction in {"ltr", "rtl"} else "ltr",
        )

        translation_id = await connection.fetchval(
            """
            INSERT INTO translations(
                language_id, source_name, source_translation_id, title, short_title,
                description, license_type, license_version, license_url,
                copyright_notice, copyright_holder, copyright_years, translated_by,
                publication_url, source_file_url, source_sha256, source_updated_at,
                imported_at, is_active, redistributable, downloadable, coverage,
                ot_books, nt_books, dc_books, book_count, verse_count,
                nonempty_verse_count, metadata
            ) VALUES(
                $1, 'BibleNLP/eBible', $2, $3, NULLIF($4, ''), $5, $6,
                NULLIF($7, ''), NULLIF($8, ''), NULLIF($9, ''), NULLIF($10, ''),
                NULLIF($11, ''), NULLIF($12, ''), NULLIF($13, ''), $14, $15,
                $16, now(), true, $17, $18, $19, $20, $21, $22, $23, $24, $25,
                $26::jsonb
            )
            ON CONFLICT (source_name, source_translation_id) DO UPDATE SET
                language_id = EXCLUDED.language_id,
                title = EXCLUDED.title,
                short_title = EXCLUDED.short_title,
                description = EXCLUDED.description,
                license_type = EXCLUDED.license_type,
                license_version = EXCLUDED.license_version,
                license_url = EXCLUDED.license_url,
                copyright_notice = EXCLUDED.copyright_notice,
                copyright_holder = EXCLUDED.copyright_holder,
                copyright_years = EXCLUDED.copyright_years,
                translated_by = EXCLUDED.translated_by,
                publication_url = EXCLUDED.publication_url,
                source_file_url = EXCLUDED.source_file_url,
                source_sha256 = EXCLUDED.source_sha256,
                source_updated_at = EXCLUDED.source_updated_at,
                imported_at = now(),
                is_active = true,
                redistributable = EXCLUDED.redistributable,
                downloadable = EXCLUDED.downloadable,
                coverage = EXCLUDED.coverage,
                ot_books = EXCLUDED.ot_books,
                nt_books = EXCLUDED.nt_books,
                dc_books = EXCLUDED.dc_books,
                book_count = EXCLUDED.book_count,
                verse_count = EXCLUDED.verse_count,
                nonempty_verse_count = EXCLUDED.nonempty_verse_count,
                metadata = EXCLUDED.metadata
            RETURNING id
            """,
            language_id,
            metadata.translation_id,
            metadata.title,
            metadata.short_title,
            metadata.description,
            license_info.license_type if license_info else "unknown",
            license_info.license_version if license_info else "",
            license_info.license_url if license_info else "",
            metadata.copyright_notice,
            license_info.copyright_holder if license_info else "",
            license_info.copyright_years if license_info else "",
            license_info.translated_by if license_info else "",
            metadata.publication_url,
            downloaded.source_url,
            downloaded.sha256,
            metadata.source_date,
            metadata.redistributable,
            metadata.downloadable,
            metadata.coverage,
            metadata.ot_books,
            metadata.nt_books,
            metadata.dc_books,
            actual_books,
            len(records),
            actual_nonempty,
            _metadata_json(metadata),
        )

        await connection.execute("DELETE FROM verses WHERE translation_id = $1", translation_id)
        rows = [
            (translation_id, book, chapter, verse, text, continuation, source_line)
            for book, chapter, verse, text, continuation, source_line in records
        ]
        for index in range(0, len(rows), batch_size):
            await connection.copy_records_to_table(
                "verses",
                records=rows[index : index + batch_size],
                columns=(
                    "translation_id",
                    "book_code",
                    "chapter",
                    "verse",
                    "text",
                    "is_range_continuation",
                    "source_line",
                ),
            )

    return {
        "translation_id": metadata.translation_id,
        "language_code": metadata.language_code,
        "database_id": translation_id,
        "books": actual_books,
        "verse_rows": len(records),
        "nonempty_verses": actual_nonempty,
        "sha256": downloaded.sha256,
        "source_url": downloaded.source_url,
    }


async def run_import(
    settings: Settings,
    *,
    refresh_catalog: bool = True,
    refresh_translations: bool = False,
) -> dict[str, Any]:
    if settings.bible_profile == "none":
        return {"status": "skipped", "reason": "BIBLE_PROFILE=none"}

    connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=30)
    source = BibleNlpSource(
        settings.source_cache_dir,
        timeout_seconds=settings.download_timeout_seconds,
    )
    run_id: int | None = None
    try:
        await seed_books(connection)
        run_id = await connection.fetchval(
            """
            INSERT INTO import_runs(profile, source_name, status)
            VALUES($1, 'BibleNLP/eBible', 'running')
            RETURNING id
            """,
            settings.bible_profile,
        )

        async with source:
            try:
                catalog = await source.fetch_catalog(refresh=refresh_catalog)
            except Exception:
                LOGGER.exception("Fresh catalog download failed; trying cached metadata")
                catalog = await source.fetch_catalog(refresh=False)

            selection: SelectionResult = select_translations(
                catalog,
                profile=settings.bible_profile,
                max_editions_per_language=settings.max_editions_per_language,
                allow_restricted=settings.allow_restricted_licenses,
                allow_unknown=settings.allow_unknown_licenses,
            )
            report = selection_summary(selection)
            await connection.execute(
                "UPDATE import_runs SET selected_count=$2, details=$3::jsonb WHERE id=$1",
                run_id,
                len(selection.selected),
                json.dumps({"selection": report}, ensure_ascii=False),
            )

            references_path = await source.fetch_references(refresh=refresh_catalog)
            references = parse_reference_lines(
                references_path.read_text(encoding="utf-8-sig").splitlines()
            )

            imported: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            failures: list[dict[str, str]] = []

            # For core/extended, a broken or empty top-ranked corpus file must not
            # eliminate the language. Try lower-ranked licensed candidates until
            # the requested number of usable editions is reached.
            groups = sorted(selection.candidates_by_language.items())
            attempt_position = 0
            total_candidates = sum(len(items) for _, items in groups)
            for language_code, candidates in groups:
                target_count = (
                    len(candidates)
                    if settings.bible_profile == "all-open"
                    else settings.max_editions_per_language
                )
                usable_count = 0
                for rank, item in enumerate(candidates, start=1):
                    if usable_count >= target_count:
                        break
                    attempt_position += 1
                    metadata = item.metadata
                    LOGGER.info(
                        "Importing %s/%s (candidate %s, attempt %s of %s)",
                        metadata.language_code,
                        metadata.translation_id,
                        rank,
                        attempt_position,
                        total_candidates,
                    )
                    try:
                        downloaded = await source.download_translation(
                            metadata,
                            refresh=refresh_translations,
                        )
                        if await _is_up_to_date(connection, downloaded):
                            skipped.append(
                                {
                                    "translation_id": metadata.translation_id,
                                    "language_code": metadata.language_code,
                                    "candidate_rank": rank,
                                    "reason": "same SHA-256 already imported",
                                }
                            )
                            usable_count += 1
                            continue
                        imported_item = await import_translation(
                            connection,
                            downloaded,
                            references,
                            batch_size=settings.import_batch_size,
                        )
                        imported_item["candidate_rank"] = rank
                        imported.append(imported_item)
                        usable_count += 1
                    except Exception as exc:  # noqa: BLE001 - continue with fallback editions
                        LOGGER.exception("Failed to import %s", metadata.translation_id)
                        failure = {
                            "translation_id": metadata.translation_id,
                            "language_code": language_code,
                            "candidate_rank": str(rank),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        failures.append(failure)
                        await connection.execute(
                            """
                            INSERT INTO import_failures(import_run_id, source_translation_id, stage, error_message)
                            VALUES($1, $2, 'translation', $3)
                            """,
                            run_id,
                            metadata.translation_id,
                            failure["error"][:4000],
                        )

            status = "succeeded" if not failures else ("partial" if imported or skipped else "failed")
            details = {
                "selection": report,
                "imported": imported,
                "skipped": skipped,
                "failures": failures,
                "cache": source.cache_status(),
            }
            await connection.execute(
                """
                UPDATE import_runs SET
                    status=$2,
                    imported_count=$3,
                    skipped_count=$4,
                    failed_count=$5,
                    details=$6::jsonb,
                    finished_at=now()
                WHERE id=$1
                """,
                run_id,
                status,
                len(imported),
                len(skipped),
                len(failures),
                json.dumps(details, ensure_ascii=False),
            )
            return {"status": status, **details}
    except Exception as exc:
        if run_id is not None:
            await connection.execute(
                """
                UPDATE import_runs SET status='failed', failed_count=failed_count+1,
                    details=jsonb_set(details, '{fatal_error}', to_jsonb($2::text), true),
                    finished_at=now()
                WHERE id=$1
                """,
                run_id,
                f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        await connection.close()
