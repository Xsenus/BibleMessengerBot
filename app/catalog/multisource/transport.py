"""HTTPS-only, host-restricted streaming cache with safe interrupted-download resume.

A local digest detects corruption, not the authenticity of an upstream publication.
No remote file is imported until UTF-8/JSON/structural validation also succeeds.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.catalog.multisource.types import ImportOptions

LOG = logging.getLogger(__name__)
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "api.github.com", "api.getbible.net",
    "bible.helloao.org", "ebible.org", "www.ebible.org", "berean.bible", "www.berean.bible"})


class DownloadError(RuntimeError):
    pass


class HttpStatusError(DownloadError):
    def __init__(self, status: int, url: str):
        self.status = status
        super().__init__(f"HTTP {status}: {url}")


class DiskLimitError(DownloadError):
    pass


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: object) -> None:
    """Atomic within the cache filesystem; a interrupted report never replaces a good one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def validate_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.port not in {None, 443} or p.username or p.password or p.fragment:
        raise ValueError("Source URL is outside the HTTPS allowlist")
    if "\\" in url or any(ord(c)<32 for c in url):
        raise ValueError("Unsafe source URL")
    if p.hostname == "raw.githubusercontent.com" and not (p.path.startswith("/BibleNLP/ebible/") or p.path.startswith("/getbible/v2/")):
        raise ValueError("Unapproved repository download")
    if p.hostname == "api.github.com" and not (p.path == "/repos/BibleNLP/ebible" or p.path.startswith("/repos/BibleNLP/ebible/")):
        raise ValueError("Unapproved GitHub metadata endpoint")
    return url


def retry_seconds(value: str | None, attempt: int) -> float:
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 2**attempt
        if 0 <= delay <= 86400:
            return min(delay, 120.0)
    return min(2**attempt, 60.0)


class Downloader:
    def __init__(self, root: Path, options: ImportOptions, client: httpx.AsyncClient | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.options = options
        self.owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(options.timeout, connect=30), follow_redirects=False,
            headers={"User-Agent": "BibleMessengerBot/1.3.1", "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        self._rate_lock = asyncio.Lock()
        self._last: dict[str, float] = {}
        self._url_locks: dict[str, asyncio.Lock] = {}
        self.requests = 0
        self.downloaded_bytes = 0
        self.cache_hits = 0
        self.resumes = 0
        self._usage = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    async def __aenter__(self) -> "Downloader":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.owns_client:
            await self.client.aclose()

    def paths(self, url: str) -> tuple[Path, Path, Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()
        directory = self.root / key[:2]
        directory.mkdir(exist_ok=True)
        target = directory / key
        return target, directory/(key+".json"), directory/(key+".part"), directory/(key+".part.json")

    async def _rate(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        async with self._rate_lock:
            delay = 1/self.options.requests_per_second - (time.monotonic()-self._last.get(host, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()

    @asynccontextmanager
    async def _stream(self, url: str, headers: dict[str, str]):
        """Validate every redirect; catalog links cannot turn this into an SSRF client."""
        current = validate_url(url)
        for _ in range(6):
            await self._rate(current)
            self.requests += 1
            async with self.client.stream("GET", current, headers=headers, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise DownloadError("Redirect without Location")
                    current = validate_url(urljoin(current, location))
                    continue
                yield response
                return
        raise DownloadError("Too many redirects")

    def _disk(self, incoming: int) -> None:
        if shutil.disk_usage(self.root).free - incoming < self.options.min_free_bytes:
            raise DiskLimitError("Free disk reserve reached; raise capacity or change IMPORT_MIN_FREE_MB")
        if self._usage + incoming > self.options.max_cache_bytes:
            raise DiskLimitError("Download cache limit reached; change IMPORT_MAX_CACHE_MB")

    def cached(self, url: str, maximum: int) -> Path | None:
        target, info, _, _ = self.paths(url)
        if not (target.is_file() and info.is_file()):
            return None
        try:
            metadata = json.loads(info.read_text())
            if metadata.get("url") != url or not 0 < target.stat().st_size <= maximum:
                return None
            if metadata.get("size") != target.stat().st_size or metadata.get("sha256") != digest_file(target):
                return None
        except (OSError, ValueError):
            return None
        return target

    async def fetch(self, url: str, *, refresh: bool = False, max_bytes: int | None = None) -> Path:
        validate_url(url)
        async with self._url_locks.setdefault(url, asyncio.Lock()):
            return await self._fetch(url, refresh=refresh, maximum=min(max_bytes or self.options.max_file_bytes, self.options.max_file_bytes))

    async def _fetch(self, url: str, *, refresh: bool, maximum: int) -> Path:
        if not refresh:
            hit = self.cached(url, maximum)
            if hit:
                self.cache_hits += 1
                return hit
        target, info, part, part_info = self.paths(url)
        error: Exception | None = None
        for attempt in range(1, self.options.attempts+1):
            self._disk(0)
            headers = {"Accept-Encoding": "identity"}
            offset = 0
            metadata: dict = {}
            if part.is_file() and part_info.is_file():
                try:
                    metadata = json.loads(part_info.read_text())
                    validator = metadata.get("validator")
                    if metadata.get("url") == url and validator and 0 < part.stat().st_size < maximum:
                        offset = part.stat().st_size
                        headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
                except (OSError, ValueError):
                    pass
            delay = retry_seconds(None, attempt)
            try:
                async with self._stream(url, headers) as response:
                    status = response.status_code
                    if status in {408, 429, 500, 502, 503, 504}:
                        delay = retry_seconds(response.headers.get("Retry-After"), attempt)
                        raise httpx.ReadError(f"Retryable HTTP {status}")
                    if status == 416 and offset:
                        self._usage -= part.stat().st_size
                        part.unlink(missing_ok=True)
                        part_info.unlink(missing_ok=True)
                        raise httpx.ReadError("Restart after rejected range")
                    if status not in {200, 206}:
                        raise HttpStatusError(status, url)
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DownloadError("Server ignored identity encoding; cannot safely resume compressed bytes")
                    append = status == 206
                    total: int | None = None
                    if append:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not offset or not match or int(match[1]) != offset or int(match[2]) < offset or int(match[2])+1 != int(match[3]):
                            # Do not repeatedly reuse a corrupt partial request.
                            if part.exists():
                                self._usage -= part.stat().st_size
                                part.unlink()
                            part_info.unlink(missing_ok=True)
                            raise DownloadError("Invalid Content-Range")
                        total = int(match[3])
                        current_etag = response.headers.get("ETag")
                        if metadata.get("etag") and current_etag and metadata["etag"] != current_etag:
                            self._usage -= part.stat().st_size
                            part.unlink(missing_ok=True)
                            part_info.unlink(missing_ok=True)
                            raise DownloadError("ETag changed during range resume; partial bytes discarded")
                        self.resumes += 1
                    else:
                        offset = 0
                        if part.exists():
                            self._usage -= part.stat().st_size
                        part.unlink(missing_ok=True)
                    length = response.headers.get("Content-Length", "")
                    expected_size = offset + int(length) if length.isdigit() else total
                    if total is not None and expected_size is not None and total != expected_size:
                        raise DownloadError("Inconsistent range length")
                    if expected_size is not None and expected_size > maximum:
                        raise DownloadError("File exceeds IMPORT_MAX_FILE_MB")
                    etag = response.headers.get("ETag", "")
                    validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
                    write_json(part_info, {"url": url, "validator": validator, "etag": etag, "size": expected_size})
                    size = offset
                    with part.open("ab" if append else "wb") as f:
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > maximum:
                                raise DownloadError("File exceeds IMPORT_MAX_FILE_MB")
                            self._disk(len(chunk))
                            f.write(chunk)
                            self._usage += len(chunk)
                            self.downloaded_bytes += len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    if not size or (expected_size is not None and size != expected_size):
                        raise httpx.ReadError("Empty or truncated response body")
                    digest = digest_file(part)
                    if target.exists():
                        self._usage -= target.stat().st_size
                    part.replace(target)
                    write_json(info, {"url": url, "sha256": digest, "size": size,
                        "etag": etag, "fetched_at": datetime.now(timezone.utc).isoformat()})
                    part_info.unlink(missing_ok=True)
                    return target
            except OSError as exc:
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise DiskLimitError('Filesystem space/quota exhausted; acquisition stopped') from exc
                raise
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                error = exc
                if attempt < self.options.attempts:
                    await asyncio.sleep(delay)
        raise DownloadError(f"Download exhausted {self.options.attempts} attempts ({type(error).__name__}): {url}")

    async def json(self, url: str, **kwargs):
        path = await self.fetch(url, **kwargs)
        # json.loads rejects invalid UTF-8; don't silently replace invalid characters.
        def unique_object(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('Duplicate JSON object key; refusing implicit overwrite')
                result[key]=value
            return result
        def invalid_constant(value):
            raise ValueError('Non-finite JSON number: '+value)
        return json.loads(path.read_text(encoding="utf-8-sig"),object_pairs_hook=unique_object,
                          parse_constant=invalid_constant), path
