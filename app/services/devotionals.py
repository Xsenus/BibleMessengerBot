"""Stable, edition-native morning/evening choices with a frozen daily plan."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.services import bible
from app.services.locks import chat_lock

SLOTS = ("morning_verse", "evening_verse")
VARIANTS = ("historical", "symbolic", "watercolor")


@lru_cache(maxsize=1)
def themes():
    return json.loads(
        (Path(__file__).resolve().parents[2] / "data/devotional_themes.json").read_text(
            encoding="utf-8"
        )
    )


def theme_for(day: date, slot: str):
    if slot not in SLOTS:
        raise ValueError("Unknown devotional slot")
    # Every week rotates the weekday's emphasis; leap day has its own ordinal.
    ordinal = day.timetuple().tm_yday
    index = (day.weekday() + (ordinal - 1) // 7) % 7
    return themes()["morning" if slot == "morning_verse" else "evening"][index]


def rank(seed: str, row) -> bytes:
    return hashlib.sha256(
        f"{seed}:{row['book_code']}:{row['chapter']}:{row['verse']}".encode()
    ).digest()


def coordinates(row):
    return row["book_code"], row["chapter"], row["verse"]


async def selection(connection, edition, chat_id: int, day: date, slot: str):
    if slot not in SLOTS:
        raise ValueError("Unknown devotional slot")
    # Always reserve morning first, so call order cannot change the two choices.
    async with chat_lock(connection, chat_id), connection.transaction():
        result = None
        for current in SLOTS[: SLOTS.index(slot) + 1]:
            existing = await connection.fetchrow(
                """SELECT * FROM daily_verse_selections
                WHERE telegram_chat_id=$1 AND translation_id=$2 AND local_date=$3 AND slot=$4""",
                chat_id,
                edition["id"],
                day,
                current,
            )
            if existing:
                result = existing
                continue
            blocked = {
                coordinates(r)
                for r in await connection.fetch(
                    """SELECT book_code,chapter,verse FROM daily_verse_selections
                WHERE telegram_chat_id=$1 AND translation_id=$2 AND local_date BETWEEN $3 AND $4""",
                    chat_id,
                    edition["id"],
                    day - timedelta(days=30),
                    day,
                )
            }
            daily = await bible.verse_of_day(connection, edition, str(chat_id), day)
            if daily:
                blocked.add(coordinates(daily))
            theme = theme_for(day, current)
            # Pick from actual text, not canonical-coordinate thematic mappings.
            candidates = await connection.fetch(
                """SELECT book_code,chapter,verse,verse_end,text FROM verses
                WHERE translation_id=$1 AND text<>'' AND NOT is_range_continuation
                AND char_length(text) BETWEEN 35 AND 450
                AND EXISTS(SELECT 1 FROM unnest($2::text[]) k WHERE search_text LIKE '%'||k||'%')
                ORDER BY book_code,chapter,verse LIMIT 2000""",
                edition["id"],
                theme["keywords"],
            )
            seed = f"devotional-v1:{chat_id}:{edition['id']}:{day.isoformat()}:{day.weekday()}:{current}"
            available = [r for r in candidates if coordinates(r) not in blocked]
            if not available:
                # Languages without curated keywords still get a native-language verse.
                candidates = await connection.fetch(
                    """SELECT book_code,chapter,verse,verse_end,text FROM verses
                    WHERE translation_id=$1 AND text<>'' AND NOT is_range_continuation
                    ORDER BY ordinal LIMIT 5000""",
                    edition["id"],
                )
                available = [r for r in candidates if coordinates(r) not in blocked]
                theme = {
                    "name": "Утреннее чтение" if current == "morning_verse" else "Вечернее чтение"
                }
            if not available:
                raise ValueError("No distinct devotional verse available")
            row = min(available, key=lambda r: rank(seed, r))
            variant = VARIANTS[
                int.from_bytes(hashlib.sha256(seed.encode()).digest()[:2], "big") % len(VARIANTS)
            ]
            result = await connection.fetchrow(
                """INSERT INTO daily_verse_selections
                (telegram_chat_id,translation_id,local_date,slot,book_code,chapter,verse,verse_text,text_sha256,theme,prompt_variant)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                chat_id,
                edition["id"],
                day,
                current,
                row["book_code"],
                row["chapter"],
                row["verse"],
                row["text"],
                hashlib.sha256(row["text"].encode()).hexdigest(),
                theme["name"],
                variant,
            )
        return result


async def selected_verse(connection, edition, chat_id, day, slot):
    chosen = await selection(connection, edition, chat_id, day, slot)
    row = await connection.fetchrow(
        """SELECT book_code,chapter,verse,verse_end,text FROM verses
        WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4""",
        edition["id"],
        chosen["book_code"],
        chosen["chapter"],
        chosen["verse"],
    )
    if not row or hashlib.sha256(row["text"].encode()).hexdigest() != chosen["text_sha256"]:
        raise ValueError("Prepared devotional source text changed; operator review required")
    return row, chosen
