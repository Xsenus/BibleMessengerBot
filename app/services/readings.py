"""Edition-native reading units, without rewriting or summarizing Scripture.

The curated Synodal catalog is deliberately scoped to its exact source edition.
Other editions use complete native chapters, never a borrowed
Psalm numbering table. Explicit /read addresses bypass automatic selection.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from functools import lru_cache
from pathlib import Path

from app.services import bible
from app.services.formatting import escape


@lru_cache(maxsize=1)
def catalog():
    return json.loads(
        (Path(__file__).resolve().parents[1] / "data/synodal_readings.json").read_text(
            encoding="utf-8"
        )
    )


def curated(edition):
    data = catalog()
    return all(edition.get(k) == data[k] for k in ("source_name", "source_translation_id"))


def complete(text):
    return bool(re.search(r'[.!?。！？׃·;;]["»”\')\]]*\s*$', text))


def fragment(text):
    """Roster entries and dependent list items are unsuitable automatic readings."""
    value = text.strip()
    return (
        len(value) < 20
        or bool(
            re.match(
                r"^(?:\d+\s|(?:первый|второй|третий|четвертый|пятый|шестой|седьмой|восьмой|девятый|десятый|одиннадцатый|двенадцатый|тринадцатый|четырнадцатый|пятнадцатый|шестнадцатый|семнадцатый|восемнадцатый|девятнадцатый|двадцатый)\b)",
                value,
                re.I,
            )
        )
        or bool(re.search(r"\b(?:сыновья|сыны)\b.*[:,].*[,;]$", value, re.I))
    )


def dependent(text):
    return bool(
        re.match(
            r"^(?:ибо\b|но\b|потому\b|посему\b|так что\b|и (?:сказал|не только|ответил)\b|он[аи]?\b|ему\b|их\b|это\b|but\b|for\b|therefore\b|because\b|and (?:he|she|they|not only)\b|he\b|she\b|they\b|this\b)",
            text.strip(),
            re.I,
        )
    )


def attach(anchor, rows):
    result = dict(anchor)
    result["reading_rows"] = [dict(r) for r in rows]
    return result


def approved_unit(anchor, rows, first, last):
    result = attach(anchor, rows)
    key = f"{anchor['book_code']}:{anchor['chapter']}:{first}:{last}"
    result['context_note'] = catalog().get('notes', {}).get(key, {})
    return result


def valid_unit(rows, first, last):
    if (
        not rows
        or rows[0]["verse"] != first
        or (rows[-1].get("verse_end") or rows[-1]["verse"]) != last
    ):
        return False
    expected = first
    for row in rows:
        if row["verse"] != expected or not row["text"]:
            return False
        expected = (row.get("verse_end") or row["verse"]) + 1
    return expected == last + 1 and sum(len(r["text"].encode()) for r in rows) < 18000


async def units(connection, edition):
    if not curated(edition):
        return []
    definitions = [
        dict(n=i, book=b, chapter=c, first=f, last=l)
        for i, (b, c, f, l) in enumerate(catalog()["units"])
    ]
    data = await connection.fetch(
        """SELECT u.n,v.book_code,v.chapter,v.verse,v.verse_end,v.text
        FROM jsonb_to_recordset($2::jsonb) AS u(n int,book text,chapter int,first int,last int)
        JOIN verses v ON v.translation_id=$1 AND v.book_code=u.book AND v.chapter=u.chapter
        AND v.verse BETWEEN u.first AND u.last AND NOT v.is_range_continuation AND v.text<>''
        ORDER BY u.n,v.verse""",
        edition["id"],
        json.dumps(definitions),
    )
    grouped = {}
    for row in data:
        grouped.setdefault(row["n"], []).append(row)
    return [
        approved_unit(rows[0], rows, definitions[n]['first'], definitions[n]['last'])
        for n, rows in grouped.items()
        if valid_unit(rows, definitions[n]["first"], definitions[n]["last"])
    ]


async def contextual(connection, edition, anchor):
    """Use reviewed meaning boundaries; punctuation alone cannot establish context."""
    if not anchor:
        return None
    rows = [
        r
        for r in await bible.chapter_rows(
            connection, edition["id"], anchor["book_code"], anchor["chapter"]
        )
        if r["text"] and not r.get("is_range_continuation", False)
    ]
    if curated(edition):
        for book, ch, first, last in catalog()["units"]:
            if (book, ch) == (anchor["book_code"], anchor["chapter"]) and first <= anchor[
                "verse"
            ] <= last:
                chosen = [r for r in rows if first <= r["verse"] <= last]
                if valid_unit(chosen, first, last):
                    return approved_unit(anchor, chosen, first, last)
        # A random claim from an unreviewed scene is not a recommended reading.
        return None
    # No verified semantic/versification map exists for this edition. Keep its
    # entire chapter rather than guessing a five-verse window in its language.
    if not rows or not any(r['verse']==anchor['verse'] for r in rows):
        return None
    if not valid_unit(rows, 1, rows[-1].get('verse_end') or rows[-1]['verse']):
        return None
    return attach(anchor, rows)


async def choose(connection, edition, *, seed=None):
    available = await units(connection, edition)
    if available:
        index = (
            secrets.randbelow(len(available))
            if seed is None
            else int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") % len(available)
        )
        return available[index]
    # Bounded indexed probes keep interactive latency independent of corpus size.
    count = edition["nonempty_verse_count"]
    for attempt in range(min(24, count)):
        ordinal = (
            secrets.randbelow(count) + 1
            if seed is None
            else int.from_bytes(hashlib.sha256(f"{seed}:{attempt}".encode()).digest()[:8], "big")
            % count
            + 1
        )
        row = await bible._ordinal_verse(connection, edition, ordinal)
        value = await contextual(connection, edition, row)
        if value:
            return value
    return None


async def daily(connection, edition, chat_id, day):
    return await choose(connection, edition, seed=f"reading-v2:{edition['id']}:{chat_id}:{day}")


async def render(connection, row, edition, locale):
    rows = row["reading_rows"]
    name = await bible._book_name(
        connection, row["book_code"], edition["language_code"], edition["id"]
    )
    first, last = rows[0]["verse"], rows[-1].get("verse_end") or rows[-1]["verse"]
    ref = str(first) if first == last else f"{first}–{last}"
    from app.services.message_languages import rows_text
    note = row.get('context_note', {}).get('ru' if locale=='ru' else 'en')
    context = f'<i>{escape(note)}</i>\n\n' if note else ''
    return rows_text(f"<b>{escape(name)} {row['chapter']}:{ref}</b>\n\n"+context, rows,
        '\n\n'+bible.attribution(edition, locale), edition, locale, separator='\n\n')
