"""Structural corpus evidence, not a claim of theological or textual proofreading."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any
from app.catalog.models import TranslationMeta, VerseReference

OT = 'GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP HAG ZEC MAL'.split()
NT = 'MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV'.split()
CORE = set(OT + NT)


@dataclass(frozen=True)
class CorpusAudit:
    """Measured physical coverage and reasons why an edition is incomplete."""
    coverage: str
    books: int
    chapters: int
    verses: int
    visible_verses: int
    range_continuations: int
    ot_books: int
    nt_books: int
    dc_books: int
    canonical_66_complete: bool
    nt_complete: bool
    missing_core_books: list[str]
    missing_reference_chapters: list[str]
    warnings: list[str]
    per_book: dict[str, dict[str, int]]
    numbering: str = 'BibleNLP Original versification; not necessarily edition-native numbering'
    verification_scope: str = 'Structural comparison with the pinned source grid, not independent proofreading'

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable audit evidence."""
        return asdict(self)


def audit_corpus(metadata: TranslationMeta, references: list[VerseReference],
                 records: list[tuple[str, int, int, str, bool, int]]) -> CorpusAudit:
    """Measure actual books/chapters; never trust a catalog's 'full' flag alone."""
    expected: dict[str, set[int]] = defaultdict(set)
    actual: dict[str, set[int]] = defaultdict(set)
    visible_counts: dict[str, int] = defaultdict(int)
    for ref in references:
        expected[ref.book_code].add(ref.chapter)
    for book, chapter, _, text, continuation, _ in records:
        if text and not continuation:
            actual[book].add(chapter)
            visible_counts[book] += 1
    if not actual:
        raise ValueError('No readable verses')
    actual_set = set(actual)
    missing_chapters = [f'{b} {c}' for b in sorted(actual) for c in sorted(expected[b] - actual[b])]
    missing_core = sorted(CORE - actual_set)
    nt_complete = set(NT) <= actual_set and all(expected[b] <= actual[b] for b in NT)
    full = CORE <= actual_set and all(expected[b] <= actual[b] for b in CORE)
    ot_complete = set(OT) <= actual_set and all(expected[b] <= actual[b] for b in OT)
    coverage = 'full' if full else 'nt' if nt_complete and not actual_set.intersection(OT) else 'ot' if ot_complete and not actual_set.intersection(NT) else 'partial'
    warnings: list[str] = []
    actual_chapters = sum(map(len, actual.values()))
    advertised_chapters = metadata.ot_chapters + metadata.nt_chapters + metadata.dc_chapters
    if metadata.total_books != len(actual):
        warnings.append(f'Catalog books={metadata.total_books}; downloaded books={len(actual)}')
    if advertised_chapters and advertised_chapters != actual_chapters:
        warnings.append(f'Catalog chapters={advertised_chapters}; source-grid chapters={actual_chapters}; versification may differ')
    if metadata.coverage == 'full' and not full:
        warnings.append('Catalog claims full Bible; downloaded structural coverage does not confirm it')
    if missing_chapters:
        warnings.append('Some chapters on the reference grid have no text; inspect edition-specific omissions')
    if metadata.total_verses and len(records) < metadata.total_verses * 0.90:
        warnings.append('Downloaded reference coverage is below 90% of catalog verse count')
    return CorpusAudit(coverage, len(actual), actual_chapters, len(records),
        sum(visible_counts.values()), sum(int(row[4]) for row in records),
        len(actual_set.intersection(OT)), len(actual_set.intersection(NT)), len(actual_set - CORE),
        full, nt_complete, missing_core, missing_chapters, warnings,
        {b: {'chapters': len(actual[b]), 'visible_verses': visible_counts[b]} for b in sorted(actual)})
