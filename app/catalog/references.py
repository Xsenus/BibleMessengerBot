"""Strict pairing of a pinned BibleNLP reference grid and a verse-per-line file."""
from __future__ import annotations
import re
from collections.abc import Iterable
from app.catalog.models import VerseReference

REFERENCE_RE = re.compile(r'^([1-4A-Z][A-Z0-9]{2})\s+(\d+):(\d+)$')


def parse_reference(value: str, line_number: int) -> VerseReference:
    """Parse one positive chapter/verse coordinate with its original source line."""
    match = REFERENCE_RE.fullmatch(value.strip())
    if not match or line_number < 1:
        raise ValueError(f'Invalid verse reference on line {line_number}: {value!r}')
    book, chapter, verse = match.groups()
    if int(chapter) < 1 or int(verse) < 1:
        raise ValueError(f'Non-positive reference on line {line_number}')
    return VerseReference(book, int(chapter), int(verse), line_number)


def parse_reference_lines(lines: Iterable[str]) -> list[VerseReference]:
    """Reject duplicates, interleaved books, and out-of-order reference coordinates."""
    result: list[VerseReference] = []
    seen: set[tuple[str, int, int]] = set()
    closed_books: set[str] = set()
    previous: VerseReference | None = None
    for index, line in enumerate(lines, start=1):
        ref = parse_reference(line, index)
        coordinate = (ref.book_code, ref.chapter, ref.verse)
        if coordinate in seen:
            raise ValueError(f'Duplicate reference on line {index}')
        if previous:
            if ref.book_code != previous.book_code:
                closed_books.add(previous.book_code)
                if ref.book_code in closed_books:
                    raise ValueError(f'Interleaved book on line {index}')
            elif (ref.chapter, ref.verse) <= (previous.chapter, previous.verse):
                raise ValueError(f'Out-of-order reference on line {index}')
        seen.add(coordinate)
        result.append(ref)
        previous = ref
    if not result:
        raise ValueError('Empty reference grid')
    return result


def pair_verses(references: list[VerseReference], text_lines: list[str]) -> list[tuple[str, int, int, str, bool, int]]:
    """Preserve missing rows as absence and validate every <range> continuation."""
    if len(references) != len(text_lines):
        raise ValueError(f'Reference/text line count mismatch: {len(references)} != {len(text_lines)}')
    records = []
    previous: VerseReference | None = None
    for reference, raw_text in zip(references, text_lines, strict=True):
        if '\ufffd' in raw_text or any(ord(c) < 32 and c not in '\t' for c in raw_text):
            raise ValueError(f'Invalid control/replacement character on line {reference.line_number}')
        text = raw_text.strip()
        if not text:
            previous = None
            continue
        continuation = text == '<range>'
        if continuation and (previous is None or previous.book_code != reference.book_code
                or previous.chapter != reference.chapter or previous.verse + 1 != reference.verse):
            raise ValueError(f'Orphan range continuation on line {reference.line_number}')
        records.append((reference.book_code, reference.chapter, reference.verse,
                        '' if continuation else text, continuation, reference.line_number))
        previous = reference
    return records


def with_ordinals(records: list[tuple[str, int, int, str, bool, int]]) -> list[tuple]:
    """Append display verse_end and dense visible ordinal for O(1)-indexed selection."""
    result: list[list] = []
    anchor: int | None = None
    ordinal = 0
    for record in records:
        if not record[4]:
            ordinal += 1
            anchor = len(result)
            result.append([*record, record[2], ordinal])
        else:
            if anchor is None:
                raise ValueError('Range without anchor')
            result[anchor][6] = record[2]
            result.append([*record, None, None])
    return [tuple(row) for row in result]
