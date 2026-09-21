"""Bible retrieval, formatting, and reading progress services."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from app.catalog.profiles import normalize_language_code
from app.services.formatting import escape


@dataclass(frozen=True, slots=True)
class TranslationChoice:
    id: int
    language_code: str
    source_id: str
    title: str
    short_title: str
    coverage: str
    license_type: str


async def list_translations(connection: asyncpg.Connection) -> list[TranslationChoice]:
    rows = await connection.fetch(
        """
        SELECT t.id, l.code AS language_code, t.source_translation_id,
               t.title, COALESCE(t.short_title, t.title) AS short_title,
               t.coverage, t.license_type
        FROM translations t
        JOIN languages l ON l.id=t.language_id
        WHERE t.is_active=true AND t.verse_count > 0
        ORDER BY l.code, t.title
        """
    )
    return [
        TranslationChoice(
            id=row["id"],
            language_code=row["language_code"],
            source_id=row["source_translation_id"],
            title=row["title"],
            short_title=row["short_title"],
            coverage=row["coverage"],
            license_type=row["license_type"],
        )
        for row in rows
    ]


async def find_translation(
    connection: asyncpg.Connection,
    identifier: str | int | None,
    *,
    preferred_language: str | None = None,
) -> asyncpg.Record | None:
    if identifier is not None:
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = await connection.fetchrow(
                """
                SELECT t.*, l.code AS language_code, l.text_direction
                FROM translations t JOIN languages l ON l.id=t.language_id
                WHERE t.id=$1 AND t.is_active=true
                """,
                int(identifier),
            )
        else:
            row = await connection.fetchrow(
                """
                SELECT t.*, l.code AS language_code, l.text_direction
                FROM translations t JOIN languages l ON l.id=t.language_id
                WHERE lower(t.source_translation_id)=lower($1) AND t.is_active=true
                """,
                str(identifier),
            )
        if row:
            return row

    candidates = [preferred_language, "rus", "eng"]
    for language in candidates:
        if not language:
            continue
        row = await connection.fetchrow(
            """
            SELECT t.*, l.code AS language_code, l.text_direction
            FROM translations t JOIN languages l ON l.id=t.language_id
            WHERE l.code=$1 AND t.is_active=true AND t.verse_count > 0
            ORDER BY (t.coverage='full') DESC, t.nonempty_verse_count DESC, t.id
            LIMIT 1
            """,
            language,
        )
        if row:
            return row
    return await connection.fetchrow(
        """
        SELECT t.*, l.code AS language_code, l.text_direction
        FROM translations t JOIN languages l ON l.id=t.language_id
        WHERE t.is_active=true AND t.verse_count > 0
        ORDER BY (t.coverage='full') DESC, t.nonempty_verse_count DESC, t.id
        LIMIT 1
        """
    )


async def user_translation(
    connection: asyncpg.Connection,
    telegram_user_id: int,
    telegram_language_code: str | None = None,
) -> asyncpg.Record | None:
    selected = await connection.fetchval(
        "SELECT default_translation_id FROM telegram_users WHERE telegram_user_id=$1",
        telegram_user_id,
    )
    language = normalize_language_code(telegram_language_code)
    return await find_translation(connection, selected, preferred_language=language)


async def chat_translation(
    connection: asyncpg.Connection,
    telegram_chat_id: int,
    *,
    fallback_language: str | None = None,
) -> asyncpg.Record | None:
    selected = await connection.fetchval(
        "SELECT default_translation_id FROM telegram_chats WHERE telegram_chat_id=$1",
        telegram_chat_id,
    )
    return await find_translation(connection, selected, preferred_language=fallback_language)


async def _book_name(
    connection: asyncpg.Connection,
    book_code: str,
    language_code: str,
) -> str:
    return await connection.fetchval(
        """
        SELECT COALESCE(
            (SELECT name FROM book_names WHERE book_code=$1 AND language_code=$2),
            (SELECT name FROM book_names WHERE book_code=$1 AND language_code='en'),
            (SELECT default_name FROM books WHERE code=$1),
            $1
        )
        """,
        book_code,
        language_code,
    )


async def render_verse(
    connection: asyncpg.Connection,
    row: asyncpg.Record,
    translation: asyncpg.Record,
    *,
    ui_language: str = "ru",
) -> str:
    book_name = await _book_name(connection, row["book_code"], ui_language)
    translation_name = translation["short_title"] or translation["title"]
    return (
        f"<blockquote>{escape(row['text'])}</blockquote>\n"
        f"<b>{escape(book_name)} {row['chapter']}:{row['verse']}</b>\n"
        f"<i>{escape(translation_name)}</i>"
    )


async def random_verse(
    connection: asyncpg.Connection,
    translation: asyncpg.Record,
) -> asyncpg.Record | None:
    # TABLESAMPLE is fast but can return no rows for small editions; random offset is stable enough here.
    count = int(translation["nonempty_verse_count"] or 0)
    if count <= 0:
        return None
    offset = await connection.fetchval("SELECT floor(random() * $1)::int", count)
    return await connection.fetchrow(
        """
        SELECT book_code, chapter, verse, text
        FROM verses
        WHERE translation_id=$1 AND text <> '' AND is_range_continuation=false
        ORDER BY book_code, chapter, verse
        OFFSET $2 LIMIT 1
        """,
        translation["id"],
        offset,
    )


async def verse_of_day(
    connection: asyncpg.Connection,
    translation: asyncpg.Record,
    seed: str,
    on_date: date | None = None,
) -> asyncpg.Record | None:
    current_date = on_date or date.today()
    count = int(translation["nonempty_verse_count"] or 0)
    if count <= 0:
        return None
    digest = hashlib.sha256(f"{current_date.isoformat()}:{seed}:{translation['id']}".encode()).digest()
    offset = int.from_bytes(digest[:8], "big") % count
    return await connection.fetchrow(
        """
        SELECT book_code, chapter, verse, text
        FROM verses
        WHERE translation_id=$1 AND text <> '' AND is_range_continuation=false
        ORDER BY book_code, chapter, verse
        OFFSET $2 LIMIT 1
        """,
        translation["id"],
        offset,
    )


async def topic_verse(
    connection: asyncpg.Connection,
    translation: asyncpg.Record,
    topic_code: str | None = None,
    seed: str = "",
    on_date: date | None = None,
) -> tuple[str, asyncpg.Record] | None:
    if topic_code:
        topic = await connection.fetchrow(
            "SELECT code, title_ru FROM topics WHERE code=$1 AND is_active=true",
            topic_code,
        )
    else:
        topics = await connection.fetch(
            "SELECT code, title_ru FROM topics WHERE is_active=true ORDER BY code"
        )
        if not topics:
            return None
        current_date = on_date or date.today()
        digest = hashlib.sha256(f"{current_date}:{seed}".encode()).digest()
        topic = topics[int.from_bytes(digest[:4], "big") % len(topics)]
    if not topic:
        return None

    rows = await connection.fetch(
        """
        SELECT v.book_code, v.chapter, v.verse, v.text
        FROM topic_references tr
        JOIN verses v ON v.translation_id=$1
            AND v.book_code=tr.book_code
            AND v.chapter=tr.chapter
            AND v.verse BETWEEN tr.verse_from AND tr.verse_to
        WHERE tr.topic_code=$2 AND v.text <> '' AND v.is_range_continuation=false
        ORDER BY tr.weight DESC, v.book_code, v.chapter, v.verse
        """,
        translation["id"],
        topic["code"],
    )
    if not rows:
        return None
    current_date = on_date or date.today()
    digest = hashlib.sha256(f"{current_date}:{seed}:{topic['code']}".encode()).digest()
    row = rows[int.from_bytes(digest[:4], "big") % len(rows)]
    return topic["title_ru"], row


async def chapter_rows(
    connection: asyncpg.Connection,
    translation_id: int,
    book_code: str,
    chapter: int,
) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        SELECT book_code, chapter, verse, text, is_range_continuation
        FROM verses
        WHERE translation_id=$1 AND book_code=$2 AND chapter=$3
        ORDER BY verse
        """,
        translation_id,
        book_code,
        chapter,
    )


async def render_chapter(
    connection: asyncpg.Connection,
    translation: asyncpg.Record,
    book_code: str,
    chapter: int,
    *,
    ui_language: str = "ru",
) -> str | None:
    rows = await chapter_rows(connection, translation["id"], book_code, chapter)
    visible = [row for row in rows if row["text"]]
    if not visible:
        return None
    book_name = await _book_name(connection, book_code, ui_language)
    body = "\n".join(f"<b>{row['verse']}</b> {escape(row['text'])}" for row in visible)
    title = escape(translation["short_title"] or translation["title"])
    return f"<b>{escape(book_name)}, глава {chapter}</b>\n<i>{title}</i>\n\n{body}"


async def first_chapter(
    connection: asyncpg.Connection,
    translation_id: int,
    *,
    testament: str | None = None,
) -> tuple[str, int] | None:
    return await connection.fetchrow(
        """
        SELECT v.book_code, MIN(v.chapter) AS chapter
        FROM verses v JOIN books b ON b.code=v.book_code
        WHERE v.translation_id=$1 AND v.text <> ''
          AND ($2::text IS NULL OR b.testament=$2)
        GROUP BY v.book_code, b.canonical_order
        ORDER BY b.canonical_order
        LIMIT 1
        """,
        translation_id,
        testament,
    )


async def next_chapter_reference(
    connection: asyncpg.Connection,
    translation_id: int,
    book_code: str | None,
    chapter: int | None,
    *,
    testament: str | None = None,
) -> tuple[str, int] | None:
    if not book_code or not chapter:
        row = await first_chapter(connection, translation_id, testament=testament)
        return (row["book_code"], row["chapter"]) if row else None

    same_book = await connection.fetchval(
        """
        SELECT MIN(chapter) FROM verses
        WHERE translation_id=$1 AND book_code=$2 AND chapter>$3 AND text <> ''
        """,
        translation_id,
        book_code,
        chapter,
    )
    if same_book:
        return book_code, int(same_book)

    current_order = await connection.fetchval("SELECT canonical_order FROM books WHERE code=$1", book_code)
    row = await connection.fetchrow(
        """
        SELECT v.book_code, MIN(v.chapter) AS chapter
        FROM verses v JOIN books b ON b.code=v.book_code
        WHERE v.translation_id=$1 AND v.text <> '' AND b.canonical_order>$2
          AND ($3::text IS NULL OR b.testament=$3)
        GROUP BY v.book_code, b.canonical_order
        ORDER BY b.canonical_order
        LIMIT 1
        """,
        translation_id,
        current_order or 0,
        testament,
    )
    if row:
        return row["book_code"], int(row["chapter"])
    return None


async def next_for_user(
    connection: asyncpg.Connection,
    telegram_user_id: int,
    translation: asyncpg.Record,
    *,
    plan_code: str = "sequential",
) -> tuple[str, int, str] | None:
    progress = await connection.fetchrow(
        """
        SELECT book_code, chapter FROM reading_progress
        WHERE telegram_user_id=$1 AND translation_id=$2 AND plan_code=$3
        """,
        telegram_user_id,
        translation["id"],
        plan_code,
    )
    testament = "NT" if plan_code == "new-testament-90" else None
    reference = await next_chapter_reference(
        connection,
        translation["id"],
        progress["book_code"] if progress else None,
        progress["chapter"] if progress else None,
        testament=testament,
    )
    if reference is None:
        return None
    book_code, chapter = reference
    rendered = await render_chapter(connection, translation, book_code, chapter)
    if rendered is None:
        return None
    await connection.execute(
        """
        INSERT INTO reading_progress(telegram_user_id, translation_id, plan_code, book_code, chapter)
        VALUES($1, $2, $3, $4, $5)
        ON CONFLICT (telegram_user_id, translation_id, plan_code) DO UPDATE SET
            book_code=EXCLUDED.book_code, chapter=EXCLUDED.chapter, updated_at=now()
        """,
        telegram_user_id,
        translation["id"],
        plan_code,
        book_code,
        chapter,
    )
    return book_code, chapter, rendered


async def search_verses(
    connection: asyncpg.Connection,
    translation_id: int,
    query: str,
    limit: int = 10,
) -> list[asyncpg.Record]:
    query = query.strip()
    if len(query) < 2:
        return []
    return await connection.fetch(
        """
        SELECT book_code, chapter, verse, text,
               similarity(search_text, lower($2)) AS rank
        FROM verses
        WHERE translation_id=$1 AND text <> ''
          AND search_text % lower($2)
        ORDER BY rank DESC
        LIMIT $3
        """,
        translation_id,
        query,
        min(max(limit, 1), 50),
    )
