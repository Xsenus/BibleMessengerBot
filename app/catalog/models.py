"""Typed catalog and import records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    source_id: str
    license_type: str
    license_version: str = ""
    license_url: str = ""
    copyright_holder: str = ""
    copyright_years: str = ""
    translated_by: str = ""
    vernacular_title: str = ""


@dataclass(frozen=True, slots=True)
class TranslationMeta:
    language_code: str
    translation_id: str
    language_name: str
    language_name_english: str
    title: str
    description: str
    redistributable: bool
    copyright_notice: str
    publication_url: str
    ot_books: int
    ot_chapters: int
    ot_verses: int
    nt_books: int
    nt_chapters: int
    nt_verses: int
    dc_books: int
    dc_chapters: int
    dc_verses: int
    text_direction: str
    downloadable: bool
    short_title: str
    script: str
    source_date: date | None
    license: LicenseInfo | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_books(self) -> int:
        return self.ot_books + self.nt_books + self.dc_books

    @property
    def total_verses(self) -> int:
        return self.ot_verses + self.nt_verses + self.dc_verses

    @property
    def coverage(self) -> str:
        if self.ot_books >= 39 and self.nt_books >= 27:
            return "full"
        if self.ot_books > 0 and self.nt_books == 0:
            return "ot"
        if self.nt_books >= 27 and self.ot_books == 0:
            return "nt"
        if self.total_books > 0:
            return "partial"
        return "unknown"


@dataclass(frozen=True, slots=True)
class VerseReference:
    book_code: str
    chapter: int
    verse: int
    line_number: int


@dataclass(frozen=True, slots=True)
class DownloadedTranslation:
    metadata: TranslationMeta
    path: Path
    source_url: str
    sha256: str
