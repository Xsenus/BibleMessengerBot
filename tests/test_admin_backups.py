"""Приватные файлы не остаются на диске после обрыва передачи, неизвестные даты снимков не подменяются датой загрузки."""
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.admin_backups import public
from app.web.api_backups import BackupResponse


def test_unknown_snapshot_name_does_not_invent_creation_date():
    record = dict(id=7, object_key='bible-messenger/v1/database/../../secret.dump.aabbccdd.aesgcm',
                  verified_at=datetime(2026, 10, 15, tzinfo=UTC), byte_count=100)
    item = public(record)
    assert item['filename'] == 'biblebot-backup-7.dump'
    assert item['created_at'] is None
    assert item['verified_at'] == '2026-10-15T00:00:00+00:00'


@pytest.mark.asyncio
async def test_file_response_removes_dump_and_unlocks_on_disconnect(tmp_path):
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    directory = Path(temporary.name)
    path = directory / 'snapshot.dump'
    path.write_bytes(b'PGDMP' + b'fixture' * 1000)
    lock = asyncio.Lock()
    await lock.acquire()
    response = BackupResponse(path, filename='fixture.dump', temporary=temporary, lock=lock)

    async def send(message):
        if message['type'] == 'http.response.body':
            raise ConnectionError('Synthetic client disconnect')

    async def receive():
        return {'type':'http.disconnect'}

    with pytest.raises(ConnectionError):
        await response({'type':'http', 'method':'POST', 'headers':[]}, receive, send)
    assert not directory.exists() and not lock.locked()
