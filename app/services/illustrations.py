"""Stored artwork and generation queue. Reading never waits for image generation."""

from __future__ import annotations

import hashlib
import logging
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


async def lookup_or_queue(connection: Any, row: Any, translation: Any) -> int | None:
    digest = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
    record = await connection.fetchrow(
        """INSERT INTO verse_illustrations(
        translation_id,book_code,chapter,verse,text_sha256) VALUES($1,$2,$3,$4,$5)
        ON CONFLICT(translation_id,book_code,chapter,verse,text_sha256)
        DO NOTHING RETURNING id,status""",
        translation["id"],
        row["book_code"],
        row["chapter"],
        row["verse"],
        digest,
    )
    if record is None:
        record = await connection.fetchrow(
            """SELECT id,status FROM verse_illustrations
            WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4 AND text_sha256=$5""",
            translation["id"],
            row["book_code"],
            row["chapter"],
            row["verse"],
            digest,
        )
    return record["id"] if record["status"] == "ready" else None


async def decorate(connection: Any, text: str, row: Any, translation: Any) -> str:
    try:
        image_id = await lookup_or_queue(connection, row, translation)
        return IllustratedText(text, image_id) if image_id else text
    except Exception as error:
        LOGGER.warning("Verse illustration unavailable (%s)", type(error).__name__)
        return text


def chunks(text: str, image_id: int | None, max_length: int = 3900) -> list:
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


async def store(connection: Any, row: Any, translation: Any, data: bytes, prompt: str) -> int:
    mime = image_type(data)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1)", lock_key("illustration-storage", 1)
        )
        await lookup_or_queue(connection, row, translation)
        digest = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        previous = await connection.fetchval(
            """SELECT COALESCE(octet_length(image_data),0) FROM verse_illustrations
            WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4 AND text_sha256=$5""",
            translation["id"],
            row["book_code"],
            row["chapter"],
            row["verse"],
            digest,
        )
        total = await connection.fetchval(
            "SELECT COALESCE(sum(octet_length(image_data)),0) FROM verse_illustrations"
        )
        if total - previous + len(data) > MAX_STORED_BYTES:
            raise ValueError(
                "Illustration storage limit exceeded; configure external storage before expanding"
            )
        return await connection.fetchval(
            """UPDATE verse_illustrations SET status='ready',image_data=$6,
            mime_type=$7,prompt=$8,telegram_file_id=NULL,telegram_bot_id=NULL,s3_key=NULL,s3_sha256=NULL,s3_backed_up_at=NULL,updated_at=now()
            WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4 AND text_sha256=$5 RETURNING id""",
            translation["id"],
            row["book_code"],
            row["chapter"],
            row["verse"],
            digest,
            data,
            mime,
            prompt[:10000],
        )


def prompt_for(row: Any, edition_title: str) -> str:
    return (
        f"Create a refined cinematic historical illustration for {edition_title}, "
        f"{row['book_code']} {row['chapter']}:{row['verse']}. Biblical text: {row['text']}\n"
        "Illustrate the meaning reverently, with historically plausible ancient surroundings. "
        "Detailed painterly realism, natural light, blue and warm gold palette, readable on a phone. "
        "No text, numbers, watermarks, modern objects, graphic violence or anthropomorphic depiction of God. "
        "For abstract passages use a restrained symbolic landscape. Image only."
    )
