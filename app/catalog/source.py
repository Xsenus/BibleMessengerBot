"""Remote BibleNLP/eBible catalog and corpus downloader."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.catalog.models import DownloadedTranslation, LicenseInfo, TranslationMeta

LOGGER = logging.getLogger(__name__)

REPOSITORY = "BibleNLP/ebible"
BRANCH = "c531ff2da02843ded6d09afbe29a197ab844981f"
SOURCE_REVISION = BRANCH
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}"
API_TREE_URL = f"https://api.github.com/repos/{REPOSITORY}/git/trees/{BRANCH}?recursive=1"
TRANSLATIONS_URL = f"{RAW_BASE}/metadata/translations.csv"
LICENSES_URL = f"{RAW_BASE}/metadata/licences.tsv"
VREF_URL = f"{RAW_BASE}/metadata/vref.txt"


def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _to_int(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _to_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "file"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BibleNlpSource:
    """Fetches metadata and verse files with a durable local cache."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeout_seconds: int = 180,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cache_dir = cache_dir / SOURCE_REVISION
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=30),
            follow_redirects=True,
            headers={
                "User-Agent": "BibleMessengerBot/1.3.0 (+https://github.com/BibleNLP/ebible)",
                "Accept": "text/plain, application/json;q=0.9, */*;q=0.1",
            },
        )
        self._corpus_index: dict[str, str] | None = None

    async def __aenter__(self) -> "BibleNlpSource":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, url: str, *, attempts: int = 4) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.get(url)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    await asyncio.sleep(min(delay, 20))
                    continue
                return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = exc
                if attempt < attempts:
                    await asyncio.sleep(min(2**attempt, 20))
        raise RuntimeError(f"Failed to download {url}: {error}")

    async def _exists(self, url: str) -> bool:
        """Check a large raw file without downloading its full body."""
        try:
            response = await self.client.head(url)
            if response.status_code == 200:
                return True
            if response.status_code not in {405, 501}:
                return False
            async with self.client.stream("GET",url,headers={"Range":"bytes=0-0"}) as response:
                return response.status_code in {200,206}
        except (httpx.TimeoutException, httpx.NetworkError):
            return False

    async def fetch_bytes_cached(
        self,
        url: str,
        cache_name: str,
        *,
        refresh: bool = False,
        max_bytes: int = 128 * 1024 * 1024,
    ) -> Path:
        target = self.cache_dir / _safe_name(cache_name)
        sidecar = target.with_suffix(target.suffix + '.sha256')
        if target.exists() and target.stat().st_size > 0 and not refresh:
            if sidecar.exists() and sidecar.read_text().strip() == _sha256(target):
                return target
            if sidecar.exists():
                raise ValueError(f'Cached source checksum mismatch: {target.name}')
            # Legacy cache is not trusted; obtain the pinned file again.

        error: Exception | None = None
        for attempt in range(4):
            temp_path: Path | None = None
            try:
                async with self.client.stream("GET",url) as response:
                    if response.status_code in {429,500,502,503,504}:
                        raise httpx.ReadError(f'Retryable HTTP {response.status_code}')
                    if response.status_code != 200:
                        raise FileNotFoundError(f'{url} returned HTTP {response.status_code}')
                    length = response.headers.get('Content-Length','')
                    if length.isdigit() and int(length)>max_bytes:
                        raise ValueError('Source exceeds configured download size')
                    with tempfile.NamedTemporaryFile(dir=self.cache_dir,delete=False) as temporary:
                        temp_path = Path(temporary.name)
                        size = 0
                        async for chunk in response.aiter_bytes(65536):
                            size += len(chunk)
                            if size>max_bytes:
                                raise ValueError('Source exceeds configured download size')
                            temporary.write(chunk)
                        temporary.flush()
                    if size==0:
                        raise ValueError('Empty source file')
                temp_path.replace(target)
                sidecar.write_text(_sha256(target)+'\n',encoding='ascii')
                return target
            except (httpx.TimeoutException,httpx.NetworkError) as exc:
                error = exc
                if attempt<3:
                    await asyncio.sleep(min(2**(attempt+1),20))
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        raise RuntimeError(f'Could not fetch pinned source: {type(error).__name__}')

    async def fetch_catalog(self, *, refresh: bool = True) -> list[TranslationMeta]:
        translations_path, licenses_path = await asyncio.gather(
            self.fetch_bytes_cached(
                TRANSLATIONS_URL,
                "translations.csv",
                refresh=refresh,
                max_bytes=8 * 1024 * 1024,
            ),
            self.fetch_bytes_cached(
                LICENSES_URL,
                "licences.tsv",
                refresh=refresh,
                max_bytes=8 * 1024 * 1024,
            ),
        )
        licenses = self._parse_licenses(licenses_path.read_text(encoding="utf-8-sig"))
        catalog = self._parse_translations(translations_path.read_text(encoding="utf-8-sig"), licenses)
        ids = [item.translation_id.lower() for item in catalog]
        if not catalog or len(ids) != len(set(ids)):
            raise ValueError('Empty catalog or duplicate edition identifiers')
        return catalog

    async def fetch_references(self, *, refresh: bool = False) -> Path:
        return await self.fetch_bytes_cached(
            VREF_URL,
            "vref.txt",
            refresh=refresh,
            max_bytes=4 * 1024 * 1024,
        )

    @staticmethod
    def _parse_licenses(text: str) -> dict[str, LicenseInfo]:
        rows = csv.DictReader(io.StringIO(text), delimiter="\t")
        result: dict[str, LicenseInfo] = {}
        for row in rows:
            source_id = (row.get("ID") or "").strip()
            if not source_id:
                continue
            item = LicenseInfo(
                source_id=source_id,
                license_type=(row.get("Licence Type") or "").strip(),
                license_version=(row.get("Licence Version") or "").strip(),
                license_url=(row.get("CC Licence Link") or "").strip(),
                copyright_holder=(row.get("Copyright Holder") or "").strip(),
                copyright_years=(row.get("Copyright Years") or "").strip(),
                translated_by=(row.get("Translation by") or "").strip(),
                vernacular_title=(row.get("Vernacular Title") or "").strip(),
            )
            for key in {
                source_id,
                source_id.lower(),
                source_id.replace("-", "_"),
                source_id.replace("_", "-"),
            }:
                result[key.lower()] = item
        return result

    @staticmethod
    def _parse_translations(
        text: str,
        licenses: dict[str, LicenseInfo],
    ) -> list[TranslationMeta]:
        rows = csv.DictReader(io.StringIO(text))
        result: list[TranslationMeta] = []
        for row in rows:
            translation_id = (row.get("translationId") or "").strip()
            language_code = (row.get("languageCode") or "").strip()
            if not translation_id or not language_code:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", translation_id) or not re.fullmatch(r"[a-z]{2,3}", language_code):
                raise ValueError("Unsafe source identifier in catalog")
            variants = [
                translation_id.lower(),
                translation_id.replace("-", "_").lower(),
                translation_id.replace("_", "-").lower(),
            ]
            license_info = next((licenses.get(key) for key in variants if key in licenses), None)
            source_date = _to_date(row.get("sourceDate") or row.get("UpdateDate") or "")
            result.append(
                TranslationMeta(
                    language_code=language_code,
                    translation_id=translation_id,
                    language_name=(row.get("languageName") or language_code).strip(),
                    language_name_english=(
                        row.get("languageNameInEnglish") or row.get("languageName") or language_code
                    ).strip(),
                    title=(row.get("title") or row.get("shortTitle") or translation_id).strip(),
                    description=(row.get("description") or "").strip(),
                    redistributable=_to_bool(row.get("Redistributable") or ""),
                    copyright_notice=(row.get("Copyright") or "").strip(),
                    publication_url=(row.get("publicationURL") or "").strip(),
                    ot_books=_to_int(row.get("OTbooks") or ""),
                    ot_chapters=_to_int(row.get("OTchapters") or ""),
                    ot_verses=_to_int(row.get("OTverses") or ""),
                    nt_books=_to_int(row.get("NTbooks") or ""),
                    nt_chapters=_to_int(row.get("NTchapters") or ""),
                    nt_verses=_to_int(row.get("NTverses") or ""),
                    dc_books=_to_int(row.get("DCbooks") or ""),
                    dc_chapters=_to_int(row.get("DCchapters") or ""),
                    dc_verses=_to_int(row.get("DCverses") or ""),
                    text_direction=(row.get("textDirection") or "ltr").strip().lower(),
                    downloadable=_to_bool(row.get("downloadable") or ""),
                    short_title=(row.get("shortTitle") or "").strip(),
                    script=(row.get("script") or "").strip(),
                    source_date=source_date,
                    license=license_info,
                    extra={
                        "homeDomain": (row.get("homeDomain") or "").strip(),
                        "dialect": (row.get("dialect") or "").strip(),
                        "Certified": (row.get("Certified") or "").strip(),
                        "swordName": (row.get("swordName") or "").strip(),
                        "font": (row.get("font") or "").strip(),
                    },
                )
            )
        return result

    async def _load_corpus_index(self) -> dict[str, str]:
        if self._corpus_index is not None:
            return self._corpus_index

        cache_path = await self.fetch_bytes_cached(
            API_TREE_URL,
            "github-tree.json",
            refresh=False,
            max_bytes=32 * 1024 * 1024,
        )
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("truncated"):
            LOGGER.warning("GitHub tree response is truncated; direct filename candidates remain enabled")
        index: dict[str, str] = {}
        for item in payload.get("tree", []):
            path = str(item.get("path", ""))
            if path.startswith("corpus/") and path.endswith(".txt"):
                index[path.lower()] = path
                index[Path(path).name.lower()] = path
        self._corpus_index = index
        return index

    @staticmethod
    def corpus_candidates(metadata: TranslationMeta) -> list[str]:
        language = metadata.language_code
        translation = metadata.translation_id
        project_variants = list(
            dict.fromkeys(
                (
                    translation,
                    translation.replace("-", "_"),
                    translation.replace("_", "-"),
                )
            )
        )
        return [f"corpus/{language}-{project}.txt" for project in project_variants]

    async def resolve_corpus_path(self, metadata: TranslationMeta) -> str:
        candidates = self.corpus_candidates(metadata)
        # Most files follow the documented naming convention, so avoid the API call first.
        for path in candidates:
            url = f"{RAW_BASE}/{path}"
            if await self._exists(url):
                return path

        index = await self._load_corpus_index()
        for path in candidates:
            found = index.get(path.lower()) or index.get(Path(path).name.lower())
            if found:
                return found

        raise FileNotFoundError(
            f"No corpus file found for {metadata.language_code}/{metadata.translation_id}"
        )

    async def download_translation(
        self,
        metadata: TranslationMeta,
        *,
        refresh: bool = False,
    ) -> DownloadedTranslation:
        corpus_path = await self.resolve_corpus_path(metadata)
        source_url = f"{RAW_BASE}/{corpus_path}"
        date_suffix = metadata.source_date.isoformat() if metadata.source_date else "undated"
        cache_name = f"{metadata.language_code}-{metadata.translation_id}-{date_suffix}.txt"
        path = await self.fetch_bytes_cached(
            source_url,
            cache_name,
            refresh=refresh,
            max_bytes=64 * 1024 * 1024,
        )
        return DownloadedTranslation(
            metadata=metadata,
            path=path,
            source_url=source_url,
            sha256=_sha256(path),
        )

    def cache_status(self) -> dict[str, Any]:
        files = [item for item in self.cache_dir.iterdir() if item.is_file()]
        return {
            "path": str(self.cache_dir),
            "files": len(files),
            "bytes": sum(item.stat().st_size for item in files),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
