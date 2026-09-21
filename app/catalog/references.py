"""Parser for the BibleNLP canonical verse reference file."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.catalog.models import VerseReference

REFERENCE_RE = re.compile(r"^([1-4A-Z][A-Z0-9]{2})\s+(\d+):(\d+)$")


def parse_reference(value: str, line_number: int) -> VerseReference:
    match = REFERENCE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid verse reference on line {line_number}: {value!r}")
    book, chapter, verse = match.groups()
    return VerseReference(book, int(chapter), int(verse), line_number)


def parse_reference_lines(lines: Iterable[str]) -> list[VerseReference]:
    return [parse_reference(line, index) for index, line in enumerate(lines, start=1)]


def pair_verses(
    references: list[VerseReference],
    text_lines: list[str],
) -> list[tuple[str, int, int, str, bool, int]]:
    if len(references) != len(text_lines):
        raise ValueError(
            f"Reference/text line count mismatch: {len(references)} != {len(text_lines)}"
        )

    records: list[tuple[str, int, int, str, bool, int]] = []
    for reference, raw_text in zip(references, text_lines, strict=True):
        text = raw_text.strip()
        if not text:
            continue
        continuation = text == "<range>"
        records.append(
            (
                reference.book_code,
                reference.chapter,
                reference.verse,
                "" if continuation else text,
                continuation,
                reference.line_number,
            )
        )
    return records
