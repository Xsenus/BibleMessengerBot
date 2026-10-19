"""Приватные файлы не остаются на диске после обрыва передачи, неизвестные даты снимков не подменяются датой загрузки."""
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.stub import Stubber

from app.services.admin_backups import public
from app.services import cloud_backup
from app.web.api_backups import BackupResponse


def test_unknown_snapshot_name_does_not_invent_creation_date():
    record = dict(id=7, object_key='bible-messenger/v1/database/../../secret.dump.aabbccdd.aesgcm',
                  verified_at=datetime(2026, 10, 15, tzinfo=UTC), byte_count=100)
    item = public(record)
    assert item['filename'] == 'biblebot-backup-7.dump'
    assert item['created_at'] is None
    assert item['verified_at'] == '2026-10-15T00:00:00+00:00'


def backup_response(tmp_path):
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    directory = Path(temporary.name)
    path = directory / 'snapshot.dump'
    path.write_bytes(b'PGDMP' + b'fixture' * 1000)
    lock = asyncio.Lock()
    return BackupResponse(path, filename='fixture.dump', temporary=temporary, lock=lock), directory, lock


@pytest.mark.asyncio
async def test_file_response_removes_dump_and_unlocks_when_send_fails(tmp_path):
    response, directory, lock = backup_response(tmp_path)
    await lock.acquire()

    async def send(message):
        if message['type'] == 'http.response.body':
            raise ConnectionError('Synthetic client disconnect')

    async def receive():
        await asyncio.Event().wait()   # клиент не сообщает об уходе: обрыв виден только по ошибке записи

    with pytest.raises(ConnectionError):
        await response({'type':'http', 'method':'POST', 'headers':[]}, receive, send)
    assert not directory.exists() and not lock.locked()


@pytest.mark.asyncio
async def test_file_response_removes_dump_and_unlocks_on_disconnect_message(tmp_path):
    # Starlette 1.8+ сам слушает http.disconnect и отменяет передачу без исключения, старые версии падали на записи;
    # в обоих случаях файл и блокировка должны освободиться.
    response, directory, lock = backup_response(tmp_path)
    await lock.acquire()

    async def send(message):
        if message['type'] == 'http.response.body':
            raise ConnectionError('Synthetic client disconnect')

    async def receive():
        return {'type':'http.disconnect'}

    try:
        await response({'type':'http', 'method':'POST', 'headers':[]}, receive, send)
    except ConnectionError:
        pass
    assert not directory.exists() and not lock.locked()


@pytest.mark.asyncio
async def test_parallel_cloud_clients_do_not_inherit_unsafe_shared_default_session(monkeypatch):
    settings = cloud_backup.CloudSettings('https://s3.fixture.invalid', 'fixture', 'access', 'secret', '12' * 32)
    shared = cloud_backup.boto3.session.Session(aws_access_key_id='access', aws_secret_access_key='secret', region_name='nl')
    def contaminated(**kwargs):
        raise RuntimeError('Shared default session must not reach independent clients')
    shared.events.register('before-parameter-build.s3.HeadObject', contaminated)
    monkeypatch.setattr(cloud_backup.boto3, 'DEFAULT_SESSION', shared)
    clients = await asyncio.gather(asyncio.to_thread(settings.client), asyncio.to_thread(settings.client))
    try:
        for client in clients:
            with Stubber(client) as stub:
                stub.add_response('head_object', {'ContentLength':123}, {'Bucket':'fixture', 'Key':'snapshot'})
                assert client.head_object(Bucket='fixture', Key='snapshot')['ContentLength'] == 123
    finally:
        for client in clients:
            client.close()
