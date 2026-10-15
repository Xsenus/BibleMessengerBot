"""Каталог проверенных облачных копий и подготовка приватного dump для скачивания администратором."""
from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boto3.s3.transfer import TransferConfig

from app.services import cloud_backup


def filename(record: Any) -> str:
    """Получить безопасное имя dump из квитанции; незнакомое имя заменить на имя с ID, без путей и управляющих символов."""
    key = str(record['object_key'])
    prefix = cloud_backup.PREFIX + 'database/'
    name = key[len(prefix):] if key.startswith(prefix) else ''
    match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]{0,180}\.dump)\.[a-f0-9]{8}\.aesgcm', name)
    return match[1] if match and '..' not in match[1] else f'biblebot-backup-{int(record["id"])}.dump'


def public(record: Any) -> dict[str, Any]:
    """Показать дату снимка из имени штатного pg_dump и дату проверки; не выдавать время загрузки за неизвестную дату создания."""
    name = filename(record)
    created = None
    match = re.fullmatch(r'biblebot-(\d{8}T\d{6}Z)-\d+\.dump', name)
    if match:
        try:
            created = datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    return {'id': record['id'], 'filename': name, 'created_at': created,
            'verified_at': record['verified_at'].isoformat(), 'size_bytes': record['byte_count']}


def prepare(record: Any, directory: Path, settings: cloud_backup.CloudSettings) -> Path:
    """Скачать только квитанцию БД, проверить размер/SHA-256 и AES-GCM, вернуть готовый PGDMP. Работать блоками на диске, не загружая dump в RAM."""
    key = str(record['object_key'])
    prefix = cloud_backup.PREFIX + 'database/'
    if (record['kind'] != 'database' or not key.startswith(prefix)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,220}\.aesgcm', key[len(prefix):])
            or '..' in key[len(prefix):]):
        raise ValueError('invalid_backup_key')
    size = int(record['byte_count'])
    if size < 34 or size > 16 * 1024**3:
        raise ValueError('invalid_backup_size')
    cloud_backup.encryption_bytes(settings.encryption_key)
    if shutil.disk_usage(directory).free < 2 * size + 64 * 1024**2:
        raise OSError('backup_disk_full')
    client = settings.client()
    try:
        head = client.head_object(Bucket=settings.bucket, Key=key)
        if head['ContentLength'] != size or head.get('Metadata', {}).get('sha256') != record['sha256']:
            raise ValueError('backup_metadata_mismatch')
        encrypted = directory / 'snapshot.aesgcm'
        client.download_file(settings.bucket, key, str(encrypted), Config=TransferConfig(
            max_concurrency=1, use_threads=False, multipart_chunksize=8 * 1024**2))
        if encrypted.stat().st_size != size or cloud_backup.digest_file(encrypted) != record['sha256']:
            raise ValueError('backup_checksum_mismatch')
        target = directory / 'snapshot.dump'
        cloud_backup.decrypt_file(encrypted, target, settings.encryption_key)
        with target.open('rb') as stream:
            if stream.read(5) != b'PGDMP':
                raise ValueError('invalid_backup_format')
        encrypted.unlink()
        return target
    finally:
        client.close()
