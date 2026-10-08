"""Upload a custom pg_dump, mirror artwork, or retrieve and decrypt a saved dump."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from app.config import Settings
from app.db import normalize_asyncpg_dsn
from app.services import cloud_backup


async def run(args):
    if args.command == "prune-local":
        return {"removed_files": cloud_backup.prune_local(args.directory)}
    settings = cloud_backup.CloudSettings.from_env()
    if args.command == "download-db":
        if not args.key.startswith(cloud_backup.PREFIX + "database/"):
            raise ValueError("Invalid database object key")
        encrypted = args.output.with_name(args.output.name + ".aesgcm")
        if args.output.exists() or encrypted.exists():
            raise ValueError("Output already exists")
        settings.client().download_file(settings.bucket, args.key, str(encrypted))
        expected = (
            settings.client()
            .head_object(Bucket=settings.bucket, Key=args.key)
            .get("Metadata", {})
            .get("sha256")
        )
        if not expected or cloud_backup.digest_file(encrypted) != expected:
            raise ValueError("Downloaded backup checksum mismatch")
        await asyncio.to_thread(
            cloud_backup.decrypt_file, encrypted, args.output, settings.encryption_key
        )
        return {
            "status": "decrypted",
            "file": str(args.output),
            "sha256": cloud_backup.digest_file(args.output),
        }
    c = await asyncpg.connect(
        normalize_asyncpg_dsn(Settings.from_env(require_bot_token=False).database_url)
    )
    try:
        if args.command == "upload-db":
            return await cloud_backup.upload_database(c, settings, args.file)
        if args.command == "images":
            return {"mirrored": await cloud_backup.backup_images(c, settings, limit=100)}
        return [
            dict(r)
            for r in await c.fetch(
                "SELECT kind,object_key,sha256,byte_count,verified_at FROM cloud_backup_receipts ORDER BY id DESC LIMIT 30"
            )
        ]
    finally:
        await c.close()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("images")
    sub.add_parser("status")
    prune = sub.add_parser("prune-local")
    prune.add_argument("--directory", type=Path, default=Path("/app/backups"))
    upload = sub.add_parser("upload-db")
    upload.add_argument("--file", type=Path, required=True)
    download = sub.add_parser("download-db")
    download.add_argument("--key", required=True)
    download.add_argument("--output", type=Path, required=True)
    try:
        print(json.dumps(asyncio.run(run(p.parse_args())), default=str))
    except Exception as e:
        # Cloud exception messages can contain signed URLs; print class only.
        print(json.dumps({"status": "failed", "error_type": type(e).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
