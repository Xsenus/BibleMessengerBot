"""Bounded configuration and interchange records for source adapters."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.catalog.audit import CorpusAudit
from app.catalog.models import DownloadedTranslation, TranslationMeta, VerseReference

SOURCE_NAMES = {"biblenlp": "BibleNLP/eBible", "getbible": "getBible/v2", "helloao": "HelloAO"}
Record = tuple[str, int, int, str, bool, int]


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("Invalid upstream identifier")
    return value


@dataclass(frozen=True)
class ImportOptions:
    sources: tuple[str, ...] = ("biblenlp", "getbible", "helloao")
    profile: str = "all-open"
    languages: tuple[str, ...] = ()
    editions: tuple[str, ...] = ()  # source:id, never an arbitrary URL
    max_editions: int = 2          # per language, per source; ignored in all-open
    refresh: bool = False
    download_only: bool = False
    min_free_bytes: int = 1024**3
    max_cache_bytes: int = 30 * 1024**3
    max_file_bytes: int = 128 * 1024**2
    timeout: int = 180
    attempts: int = 5
    requests_per_second: float = 2.0
    allow_replace: bool = False

    def __post_init__(self) -> None:
        if not self.sources or len(set(self.sources)) != len(self.sources) or set(self.sources) - SOURCE_NAMES.keys():
            raise ValueError("BIBLE_SOURCES must be a unique comma-separated subset of biblenlp,getbible,helloao")
        if self.profile not in {"core", "extended", "all-open", "none"}:
            raise ValueError("Invalid import profile")
        if self.max_editions < 1 or self.max_editions > 100:
            raise ValueError("max_editions must be 1..100")
        if self.min_free_bytes < 0 or self.max_cache_bytes < 1 or not 1024 <= self.max_file_bytes <= 512*1024**2:
            raise ValueError("Invalid disk/file limits")
        if not 1 <= self.attempts <= 10 or not 10 <= self.timeout <= 1800:
            raise ValueError("Invalid network retry/timeout limits")
        if not 0.1 <= self.requests_per_second <= 10:
            raise ValueError("Network rate must be finite and 0.1..10 requests/second")
        for code in self.languages:
            if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", code):
                raise ValueError("Invalid language filter")
        for key in self.editions:
            source, sep, identifier = key.partition(":")
            if not sep or source not in self.sources:
                raise ValueError("Edition filter must be enabled-source:id")
            safe_id(identifier)

    @classmethod
    def from_env(cls, **changes: Any) -> "ImportOptions":
        def csv(name: str, default: str = "") -> tuple[str, ...]:
            return tuple(s.strip() for s in os.getenv(name, default).split(",") if s.strip())
        values: dict[str, Any] = dict(
            sources=csv("BIBLE_SOURCES", "biblenlp,getbible,helloao"),
            profile=os.getenv("BIBLE_PROFILE", "all-open"),
            languages=csv("IMPORT_LANGUAGES"), editions=csv("IMPORT_EDITIONS"),
            max_editions=int(os.getenv("MAX_EDITIONS_PER_LANGUAGE", "2")),
            min_free_bytes=int(os.getenv("IMPORT_MIN_FREE_MB", "1024"))*1024**2,
            max_cache_bytes=int(os.getenv("IMPORT_MAX_CACHE_MB", "30720"))*1024**2,
            max_file_bytes=int(os.getenv("IMPORT_MAX_FILE_MB", "128"))*1024**2,
            timeout=int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180")),
            attempts=int(os.getenv("IMPORT_RETRIES", "5")),
            requests_per_second=float(os.getenv("IMPORT_REQUESTS_PER_SECOND", "2")),
        )
        values.update(changes)
        return cls(**values)


@dataclass(frozen=True)
class Candidate:
    source: str
    metadata: TranslationMeta
    url: str
    revision: str
    numbering: str
    raw: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.metadata.translation_id}"

    @property
    def source_name(self) -> str:
        return SOURCE_NAMES[self.source]


@dataclass
class Prepared:
    candidate: Candidate
    downloaded: DownloadedTranslation
    references: list[VerseReference]
    records: list[Record]
    audit: CorpusAudit
    content_sha256: str
    book_names: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
