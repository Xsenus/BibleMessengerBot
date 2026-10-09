"""Private, immutable S3 copies with read-back verification and encrypted DB dumps."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"BMBK1"
PREFIX = "bible-messenger/v1/"


@dataclass(frozen=True)
class CloudSettings:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    encryption_key: str = field(repr=False)
    region: str = "nl"

    @classmethod
    def from_env(cls):
        s = cls(
            os.getenv("S3_ENDPOINT", ""),
            os.getenv("S3_BUCKET", ""),
            os.getenv("S3_ACCESS_KEY", ""),
            os.getenv("S3_SECRET_KEY", ""),
            os.getenv("BACKUP_ENCRYPTION_KEY", ""),
            os.getenv("S3_REGION", "nl"),
        )
        if s.endpoint:
            parsed = urlparse(s.endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("S3 endpoint must be an HTTPS origin")
            if not s.bucket or not s.access_key or not s.secret_key:
                raise ValueError("Incomplete S3 configuration")
        return s

    @property
    def enabled(self):
        return bool(self.endpoint and self.bucket)

    def client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"mode": "standard", "max_attempts": 3},
                connect_timeout=15,
                read_timeout=120,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while block := f.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def encryption_bytes(value):
    try:
        key = bytes.fromhex(value)
    except ValueError as e:
        raise ValueError("Invalid backup encryption key") from e
    if len(key) != 32:
        raise ValueError("Backup encryption key must contain 32 bytes (64 hex characters)")
    return key


def encrypt_file(source, target, key):
    nonce = secrets.token_bytes(12)
    cipher = Cipher(algorithms.AES(encryption_bytes(key)), modes.GCM(nonce)).encryptor()
    target = Path(target)
    with Path(source).open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        dst.write(MAGIC + nonce)
        cipher.authenticate_additional_data(MAGIC)
        while block := src.read(1024 * 1024):
            dst.write(cipher.update(block))
        dst.write(cipher.finalize())
        dst.write(cipher.tag)
    return target


def decrypt_file(source, target, key):
    source, target = Path(source), Path(target)
    temporary = target.with_name(target.name + ".part")
    if target.exists() or temporary.exists():
        raise ValueError("Restore target already exists")
    try:
        with source.open("rb") as src:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError("Unknown encrypted backup format")
            nonce = src.read(12)
            size = source.stat().st_size - len(MAGIC) - 12 - 16
            if size < 1:
                raise ValueError("Truncated encrypted backup")
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(len(MAGIC) + 12)
            cipher = Cipher(
                algorithms.AES(encryption_bytes(key)), modes.GCM(nonce, tag)
            ).decryptor()
            cipher.authenticate_additional_data(MAGIC)
            with temporary.open("xb") as dst:
                os.chmod(temporary, 0o600)
                remaining = size
                while remaining:
                    block = src.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Truncated encrypted backup")
                    dst.write(cipher.update(block))
                    remaining -= len(block)
                dst.write(cipher.finalize())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def upload_checked(
    client, bucket, key, data, *, content_type="application/octet-stream", metadata=None
):
    if not key.startswith(PREFIX):
        raise ValueError("Object is outside application prefix")
    if isinstance(data, Path):
        sha = digest_file(data)
        size = data.stat().st_size
        if size >= 8 * 1024**2:
            client.upload_file(
                str(data),
                bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {"sha256": sha, **(metadata or {})},
                },
                Config=TransferConfig(
                    multipart_threshold=8 * 1024**2,
                    multipart_chunksize=8 * 1024**2,
                    max_concurrency=1,
                    use_threads=False,
                ),
            )
        else:
            with data.open("rb") as body:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentLength=size,
                    ContentType=content_type,
                    Metadata={"sha256": sha, **(metadata or {})},
                )
    else:
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentMD5=base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
            ContentType=content_type,
            Metadata={"sha256": sha, **(metadata or {})},
        )
    head = client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha:
        raise ValueError("S3 object metadata verification failed")
    result = client.get_object(Bucket=bucket, Key=key)
    actual = hashlib.sha256()
    try:
        while block := result["Body"].read(1024 * 1024):
            actual.update(block)
    finally:
        result["Body"].close()
    if actual.hexdigest() != sha:
        raise ValueError("S3 read-back checksum mismatch")
    return {"object_key": key, "sha256": sha, "byte_count": size}


async def backup_images(connection, settings: CloudSettings, *, limit=20):
    import asyncio

    if not settings.enabled:
        return 0
    client = settings.client()
    rows = await connection.fetch(
        """SELECT i.id,i.image_data,i.mime_type,i.text_sha256,i.translation_id,i.book_code,i.chapter,i.verse,i.prompt,i.generated_at,i.prompt_version,i.artwork_scope,
        t.source_name,t.source_translation_id,t.source_sha256 FROM verse_illustrations i JOIN translations t ON t.id=i.translation_id
        WHERE i.status='ready' AND i.s3_backed_up_at IS NULL ORDER BY i.id LIMIT $1""",
        limit,
    )
    for row in rows:
        data = bytes(row["image_data"])
        sha = hashlib.sha256(data).hexdigest()
        suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[row["mime_type"]]
        key = PREFIX + "images/" + sha + "." + suffix
        receipt = await asyncio.to_thread(
            upload_checked, client, settings.bucket, key, data, content_type=row["mime_type"]
        )
        # Record the Bible coordinates separately so images survive DB loss too.
        manifest = {
            k: row[k]
            for k in [
                "id",
                "translation_id",
                "book_code",
                "chapter",
                "verse",
                "text_sha256",
                "prompt",
                "source_name",
                "source_translation_id",
                "source_sha256",
            ]
        }
        manifest.update(receipt)
        manifest["generated_at"] = row["generated_at"].isoformat() if row["generated_at"] else None
        manifest["prompt_version"] = row["prompt_version"]
        manifest["artwork_scope"] = row.get("artwork_scope", "verse")
        await asyncio.to_thread(
            upload_checked,
            client,
            settings.bucket,
            PREFIX + "image-manifests/" + str(row["id"]) + "-" + sha + ".json",
            json.dumps(manifest).encode(),
            content_type="application/json",
        )
        async with connection.transaction():
            await connection.execute(
                """UPDATE verse_illustrations SET s3_key=$2,s3_sha256=$3,s3_backed_up_at=now()
                WHERE id=$1 AND image_data=$4""",
                row["id"],
                key,
                sha,
                data,
            )
            await record_receipt(connection, "image", receipt)
    return len(rows)


async def record_receipt(connection, kind, receipt):
    await connection.execute(
        """INSERT INTO cloud_backup_receipts(kind,object_key,sha256,byte_count)
        VALUES($1,$2,$3,$4) ON CONFLICT(object_key) DO UPDATE SET verified_at=now()""",
        kind,
        receipt["object_key"],
        receipt["sha256"],
        receipt["byte_count"],
    )


def prune_local(directory: Path, *, keep=7, maximum_bytes=4 * 1024**3):
    """Only our own regular snapshot files; retain the newest snapshot at minimum."""
    import re

    root = directory.resolve()
    groups = []
    for path in root.glob("biblebot-*.dump"):
        if not re.fullmatch(r"biblebot-\d{8}T\d{6}Z-\d+\.dump", path.name) or path.is_symlink():
            continue
        companions = [
            path,
            path.with_name(path.name + ".sha256"),
            *root.glob(path.name + ".*.aesgcm"),
        ]
        files = [
            p
            for p in companions
            if p.is_file() and not p.is_symlink() and p.resolve().parent == root
        ]
        groups.append((path.stat().st_mtime, files, sum(p.stat().st_size for p in files)))
    groups.sort(reverse=True, key=lambda g: g[0])
    total = 0
    removed = 0
    for index, (_, files, size) in enumerate(groups):
        if index == 0 or (index < keep and total + size <= maximum_bytes):
            total += size
            continue
        for path in files:
            path.unlink()
            removed += 1
    return removed


async def upload_database(connection, settings: CloudSettings, path: Path):
    import asyncio

    if not settings.enabled:
        return {"status": "disabled"}
    with path.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise ValueError("Use a PostgreSQL custom-format dump")
    encryption_bytes(settings.encryption_key)
    encrypted = path.with_name(path.name + "." + secrets.token_hex(4) + ".aesgcm")
    await asyncio.to_thread(encrypt_file, path, encrypted, settings.encryption_key)
    key = PREFIX + "database/" + encrypted.name
    receipt = await asyncio.to_thread(
        upload_checked, settings.client(), settings.bucket, key, encrypted
    )
    await record_receipt(connection, "database", receipt)
    return {"status": "verified", **receipt, "local_encrypted_file": str(encrypted)}
