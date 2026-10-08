"""Append-only verse artwork, shared freshness and per-chat archive rotation."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from app.services.formatting import plain_text, split_message, utf16_length
from app.services.locks import lock_key

LOGGER = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_STORED_BYTES = 2 * 1024**3


class IllustratedText(str):
    image_id: int

    def __new__(cls, text: str, image_id: int):
        instance = super().__new__(cls, text)
        instance.image_id = image_id
        return instance


class ReadingText(str):
    """One editable reading card, including text while its artwork is pending."""

    def __new__(cls, text, image_id=None, request_id=None):
        instance = super().__new__(cls, text)
        instance.image_id, instance.request_id = image_id, request_id
        return instance


class ChapterText(ReadingText):
    """Full chapter, with one editable first card and lossless continuations."""

    def __new__(cls, text, first, continuations):
        instance = super().__new__(cls, text, first.image_id, first.request_id)
        instance.first, instance.continuations = first, continuations
        return instance


def chapter_source(rows):
    visible = [dict(r) for r in rows if r['text'] and not r.get('is_range_continuation')]
    if not visible:
        return None
    return dict(visible[0], artwork_scope='chapter', reading_rows=visible,
                text='\n'.join(f"[{r['verse']}-{r.get('verse_end') or r['verse']}] {r['text']}" for r in visible))


async def source_row(connection, image):
    """Rebuild the exact source that defines this verse or complete chapter."""
    from app.services import bible

    if image.get('artwork_scope', 'verse') == 'chapter':
        return chapter_source(await bible.chapter_rows(connection, image['translation_id'], image['book_code'], image['chapter']))
    return await connection.fetchrow(
        "SELECT book_code,chapter,verse,verse_end,text FROM verses WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4 AND text<>'' AND NOT is_range_continuation",
        image['translation_id'], image['book_code'], image['chapter'], image['verse'])


async def valid_request_source(connection, request, *, image=None, edition=None):
    """Validate every frozen verse both when queueing and just before editing."""
    from app.services import bible

    image = image or await connection.fetchrow('SELECT * FROM verse_illustrations WHERE id=$1',request['image_id'])
    if not image or image['status']!='ready':
        return False
    edition = edition or await bible.find_translation(connection,image['translation_id'])
    if not edition:
        return False
    source = await source_row(connection,image)
    if not source or identity(source,edition)[4]!=image['text_sha256']:
        return False
    snapshot=request['source_snapshot']
    snapshot=json.loads(snapshot) if isinstance(snapshot,str) else snapshot
    if not snapshot:
        return False
    for item in snapshot:
        native=await connection.fetchrow('SELECT text,verse_end FROM verses WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4',
            image['translation_id'],image['book_code'],image['chapter'],item['verse'])
        if not native or hashlib.sha256(native['text'].encode()).hexdigest()!=item['sha256'] or (native['verse_end'] or item['verse'])!=item['verse_end']:
            return False
    return True


async def decorate_chapter(connection, text, translation, book_code, chapter, *, chat,
                           request_key, thread_id=None, max_length=3900, rows=None):
    from app.services import bible

    row = chapter_source(rows if rows is not None else await bible.chapter_rows(connection, translation['id'], book_code, chapter))
    if not row:
        return text
    fits = len(text.encode('utf-8')) <= 24000 and (max_length >= 3900 or utf16_length(plain_text(text)) <= max_length)
    parts = [str(text)] if fits else split_message(text, min(max_length, 3900))
    first = await decorate(connection, parts[0], row, translation, chat=chat,
                           request_key=request_key, thread_id=thread_id)
    if not isinstance(first, ReadingText):
        return text
    return ChapterText(text, first, parts[1:])


async def bind_message(connection, chat_id, request_id, message_id):
    """Only a confirmed initial send may give a background edit its target."""
    if request_id is not None:
        await connection.execute(
            """UPDATE illustration_requests SET telegram_message_id=$3
            WHERE id=$1 AND telegram_chat_id=$2 AND state='waiting'
            AND (telegram_message_id IS NULL OR telegram_message_id=$3)""",
            request_id,
            chat_id,
            message_id,
        )


def identity(row, translation):
    return (
        translation["id"],
        row["book_code"],
        row["chapter"],
        row["verse"],
        hashlib.sha256((('chapter\0' if row.get('artwork_scope') == 'chapter' else '') + row["text"]).encode("utf-8")).hexdigest(),
    )


@asynccontextmanager
async def version_lock(connection, row, translation):
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1)",
            lock_key("illustration-verse", identity(row, translation)),
        )
        yield


async def pending(connection, row, translation):
    """One open version per exact source text, including concurrent callers."""
    key = identity(row, translation)
    await connection.execute(
        """INSERT INTO verse_illustrations(translation_id,book_code,chapter,verse,text_sha256,artwork_scope)
        VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(translation_id,book_code,chapter,verse,text_sha256)
        WHERE status='pending' DO NOTHING""",
        key[0],
        key[1],
        key[2],
        key[3],
        key[4],
        row.get('artwork_scope', 'verse'),
    )
    return await connection.fetchrow(
        """SELECT * FROM verse_illustrations WHERE translation_id=$1 AND book_code=$2
        AND chapter=$3 AND verse=$4 AND text_sha256=$5 AND status='pending'""",
        key[0],
        key[1],
        key[2],
        key[3],
        key[4],
    )


async def recent(connection, row, translation):
    """Calendar months; Telegram cache updates never renew creation age."""
    key = identity(row, translation)
    return await connection.fetchval(
        """SELECT id FROM verse_illustrations WHERE translation_id=$1 AND book_code=$2
        AND chapter=$3 AND verse=$4 AND text_sha256=$5 AND status='ready'
        AND generated_at > now()-interval '3 months'
        ORDER BY generated_at DESC,id DESC LIMIT 1""",
        key[0],
        key[1],
        key[2],
        key[3],
        key[4],
    )


async def lookup_or_queue(
    connection: Any, row: Any, translation: Any, *, chat_id: int | None = None
) -> int | None:
    async with version_lock(connection, row, translation):
        key = identity(row, translation)
        record = await connection.fetchrow(
            """SELECT i.id FROM verse_illustrations i
            LEFT JOIN illustration_views h ON h.image_id=i.id AND h.telegram_chat_id=$6
            WHERE i.translation_id=$1 AND i.book_code=$2 AND i.chapter=$3 AND i.verse=$4
            AND i.text_sha256=$5 AND i.status='ready'
            ORDER BY (h.image_id IS NULL) DESC,h.last_sent_at ASC NULLS FIRST,h.send_count ASC NULLS FIRST,
            i.generated_at DESC,i.id DESC LIMIT 1""",
            key[0],
            key[1],
            key[2],
            key[3],
            key[4],
            chat_id,
        )
        if not await recent(connection, row, translation):
            await pending(connection, row, translation)
        return record["id"] if record else None


async def record_view(connection, chat_id, image_id):
    await connection.execute(
        """INSERT INTO illustration_views(telegram_chat_id,image_id) VALUES($1,$2)
        ON CONFLICT(telegram_chat_id,image_id) DO UPDATE SET last_sent_at=now(),
        send_count=illustration_views.send_count+1""",
        chat_id,
        image_id,
    )


async def decorate(
    connection: Any,
    text: str,
    row: Any,
    translation: Any,
    *,
    chat=None,
    request_key=None,
    thread_id=None,
) -> str:
    request_id = None
    try:
        image_id = await lookup_or_queue(
            connection, row, translation, chat_id=chat["telegram_chat_id"] if chat else None
        )
        if chat is not None and request_key is not None:
            from app.services import artwork

            queued = await artwork.request_image(
                connection, row, translation, chat, str(text), request_key, thread_id
            )
            if not queued and image_id is None:
                # A concurrent generator may have finished after the first lookup.
                image_id = await lookup_or_queue(
                    connection, row, translation, chat_id=chat["telegram_chat_id"]
                )
            if queued:
                request_id = await connection.fetchval(
                    "SELECT id FROM illustration_requests WHERE telegram_chat_id=$1 AND request_key=$2 AND state='waiting'",
                    chat["telegram_chat_id"],
                    request_key,
                )
        if chat is not None:
            return ReadingText(text, image_id, request_id)
        return IllustratedText(text, image_id) if image_id else text
    except Exception as error:
        LOGGER.warning("Verse illustration unavailable (%s)", type(error).__name__)
        return text


def chunks(text: str, image_id: int | None, max_length: int = 3900) -> list:
    if isinstance(text, ChapterText):
        return [*chunks(text.first, text.image_id, max_length), *text.continuations]
    if isinstance(text, ReadingText):
        image_id = text.image_id if image_id is None else image_id
        if len(text.encode("utf-8")) > 24000:
            raise ValueError("Reading card exceeds the bounded rich-message size")
        return [
            {"kind": "rich", "text": str(text), "image_id": image_id, "request_id": text.request_id}
        ]
    parts = split_message(text, max_length)
    if not image_id:
        return parts
    if utf16_length(plain_text(text)) <= 1024:
        return [{"kind": "photo", "image_id": image_id, "caption": str(text)}]
    return [{"kind": "photo", "image_id": image_id, "caption": ""}, *parts]


def image_type(data: bytes) -> str:
    if not 1 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Image must be 1 byte to 5 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Use a PNG, JPEG or WebP image")


async def store(
    connection: Any,
    row: Any,
    translation: Any,
    data: bytes,
    prompt: str,
    *,
    image_id=None,
    prompt_version=1,
) -> int:
    """Fill an explicit pending version; never replace any ready binary."""
    mime = image_type(data)
    key = identity(row, translation)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1)", lock_key("illustration-storage", 1)
        )
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1)", lock_key("illustration-verse", key)
        )
        total = await connection.fetchval(
            "SELECT COALESCE(sum(octet_length(image_data)),0) FROM verse_illustrations"
        )
        if total + len(data) > MAX_STORED_BYTES:
            raise ValueError(
                "Illustration storage limit exceeded; configure external storage before expanding"
            )
        if image_id is None:
            # A manual import is a new version if all current versions are ready.
            target = await pending(connection, row, translation)
            image_id = target["id"]
        identifier = await connection.fetchval(
            """UPDATE verse_illustrations SET status='ready',image_data=$7,mime_type=$8,
            prompt=$9,generated_at=now(),prompt_version=$10,updated_at=now()
            WHERE id=$6 AND translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4
            AND text_sha256=$5 AND status='pending' RETURNING id""",
            key[0],
            key[1],
            key[2],
            key[3],
            key[4],
            image_id,
            data,
            mime,
            prompt if row.get('artwork_scope') == 'chapter' else prompt[:10000],
            prompt_version,
        )
        if identifier is None:
            raise ValueError("Image version is no longer pending; existing artwork preserved")
        return identifier


def prompt_for(row: Any, edition_title: str) -> str:
    from app.services.artwork import prompt

    return prompt(row, {"title": edition_title}, slot="on_demand", variant="symbolic")
