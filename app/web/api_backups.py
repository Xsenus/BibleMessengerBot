"""Защищённый список резервных копий и скачивание проверенного расшифрованного dump из S3."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Path as ApiPath
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from app.services import admin_backups, cloud_backup


class BackupResponse(FileResponse):
    """Передать dump блоками и удалить временные файлы даже при обрыве соединения; освободить единственное место скачивания."""

    def __init__(self, path: Any, *, filename: str, temporary: Any, lock: asyncio.Lock):
        """Принять подготовленный приватный файл и владение его временным каталогом и блокировкой."""
        super().__init__(path, filename=filename, media_type='application/octet-stream',
                         headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        self.temporary = temporary
        self.lock = lock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Гарантировать очистку после успешной передачи, отмены или ошибки ASGI."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.temporary.cleanup()
            finally:
                self.lock.release()


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    """Подключить каталог и скачивание только с X-Admin-Key; одна загрузка за раз ограничивает место на диске."""
    router = APIRouter(prefix='/api/admin/backups', dependencies=[Depends(auth)])
    download_lock = asyncio.Lock()
    probe_slots = asyncio.Semaphore(3)

    async def connection():
        """Выдать соединение для короткого чтения квитанций; скачивание не удерживает его."""
        pool = await pool_factory()
        async with pool.acquire() as conn:
            yield conn

    Conn = Depends(connection)

    async def receipt(identifier: int) -> dict:
        """Прочитать квитанцию БД и освободить соединение до сетевых запросов."""
        pool = await pool_factory()
        async with pool.acquire() as conn:
            record = await conn.fetchrow("SELECT * FROM cloud_backup_receipts WHERE id=$1 AND kind='database'", identifier)
        if not record:
            raise HTTPException(404, 'Резервная копия не найдена')
        return dict(record)

    def cloud_settings() -> cloud_backup.CloudSettings:
        """Не запускать проверки или скачивание без S3 и корректного ключа шифрования."""
        try:
            settings = cloud_backup.CloudSettings.from_env()
            if not settings.enabled:
                raise ValueError('disabled')
            cloud_backup.encryption_bytes(settings.encryption_key)
            return settings
        except ValueError:
            raise HTTPException(503, 'Облачное хранилище или ключ расшифровки не настроены') from None

    @router.get('/{identifier}/availability')
    async def availability(identifier: int = ApiPath(ge=1, le=9223372036854775807)) -> dict:
        """Обновить наличие файла отдельно от быстрого списка, максимум три HEAD одновременно; не выдавать сетевые ошибки за удаление архива."""
        record = await receipt(identifier)
        settings = cloud_settings()
        status = 'available'
        try:
            async with probe_slots:
                await run_in_threadpool(admin_backups.probe, record, settings)
        except ClientError as error:
            status = 'missing' if error.response.get('Error', {}).get('Code') in {'404','NoSuchKey','NotFound'} else 'unavailable'
        except ValueError:
            status = 'invalid'
        except Exception:
            status = 'unavailable'
        return {'id': identifier, 'status': status}

    @router.get('')
    async def backups(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000),
                      conn: Any = Conn) -> dict:
        """Вернуть страницу квитанций БД, дату снимка и проверки, размер и доступность облачного скачивания."""
        try:
            settings = cloud_backup.CloudSettings.from_env()
            configured = settings.enabled
            if configured:
                cloud_backup.encryption_bytes(settings.encryption_key)
        except ValueError:
            configured = False
        rows = await conn.fetch("SELECT * FROM cloud_backup_receipts WHERE kind='database' ORDER BY id DESC LIMIT $1 OFFSET $2", limit, offset)
        total = await conn.fetchval("SELECT count(*) FROM cloud_backup_receipts WHERE kind='database'")
        return {'items': [admin_backups.public(r) for r in rows], 'total': total,
                'limit': limit, 'offset': offset, 'download_enabled': configured}

    @router.post('/{identifier}/download')
    async def download(identifier: int = ApiPath(ge=1, le=9223372036854775807)) -> BackupResponse:
        """Проверить квитанцию и архив, затем передать .dump. Никогда не восстанавливать рабочую БД и не раскрывать подписанные URL/ключи при ошибках."""
        record = await receipt(identifier)
        settings = cloud_settings()
        if download_lock.locked():
            raise HTTPException(409, 'Другая копия уже скачивается. Дождитесь завершения и повторите.')
        await download_lock.acquire()
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix='biblebot-admin-backup-')
            from pathlib import Path
            path = await run_in_threadpool(admin_backups.prepare, dict(record), Path(temporary.name), settings)
            return BackupResponse(path, filename=admin_backups.filename(record), temporary=temporary, lock=download_lock)
        except BaseException as error:
            try:
                if temporary is not None:
                    temporary.cleanup()
            finally:
                download_lock.release()
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                raise HTTPException(404, 'Файл резервной копии отсутствует в хранилище') from None
            if isinstance(error, Exception):
                raise HTTPException(502, 'Не удалось скачать или проверить архив. Файл не выдан; попробуйте позже.') from None
            raise

    return router
