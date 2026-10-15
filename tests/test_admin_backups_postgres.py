"""Настоящие SQL-квитанции и AES-GCM; сеть S3 синтетическая, рабочие базы и архивы не используются."""
import asyncio
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.services import admin_backups, cloud_backup
from app.web import api_backups
from tests.test_admin_api_postgres import H, make
from tests.test_postgres_integration import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


async def fixture(c, tmp_path, monkeypatch, payload=None):
    key = '12' * 32
    for name, value in {'S3_ENDPOINT':'https://s3.fixture.invalid', 'S3_BUCKET':'fixture',
                        'S3_ACCESS_KEY':'fixture-access', 'S3_SECRET_KEY':'fixture-secret',
                        'BACKUP_ENCRYPTION_KEY':key}.items():
        monkeypatch.setenv(name, value)
    payload = payload or b'PGDMP' + b'SYNTHETIC NOT A PRODUCTION BACKUP' * 1000
    source = tmp_path / 'fixture.dump'
    source.write_bytes(payload)
    encrypted = cloud_backup.encrypt_file(source, tmp_path / 'fixture.aesgcm', key)
    cipher = encrypted.read_bytes()
    object_key = cloud_backup.PREFIX + 'database/biblebot-20261015T024146Z-123.dump.abcdef12.aesgcm'
    identifier = await c.fetchval("""INSERT INTO cloud_backup_receipts(kind,object_key,sha256,byte_count)
        VALUES('database',$1,$2,$3) RETURNING id""", object_key, hashlib.sha256(cipher).hexdigest(), len(cipher))

    class Client:
        def __init__(self):
            self.downloads = 0
            self.closed = False
            self.corrupt = False
            self.missing = False

        def head_object(self, **kwargs):
            assert kwargs == {'Bucket':'fixture', 'Key':object_key}
            if self.missing:
                raise ClientError({'Error':{'Code':'NoSuchKey', 'Message':'fixture-secret https://signed.fixture.invalid/?secret'}}, 'HeadObject')
            return {'ContentLength':len(cipher), 'Metadata':{'sha256':hashlib.sha256(cipher).hexdigest()}}

        def download_file(self, bucket, name, path, **kwargs):
            assert bucket == 'fixture' and name == object_key
            assert kwargs['Config'].use_threads is False
            self.downloads += 1
            Path(path).write_bytes(cipher[:-1] + bytes([cipher[-1] ^ 1]) if self.corrupt else cipher)

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(cloud_backup.CloudSettings, 'client', lambda _:client)
    original = tempfile.TemporaryDirectory
    directories = []

    def temporary(**kwargs):
        folder = original(dir=tmp_path, **kwargs)
        directories.append(Path(folder.name))
        return folder

    monkeypatch.setattr(api_backups.tempfile, 'TemporaryDirectory', temporary)
    return identifier, payload, client, directories


async def test_backup_catalog_and_download_require_header_key(db, monkeypatch, tmp_path):
    identifier, _, s3, _ = await fixture(db[0], tmp_path, monkeypatch)
    async with make(monkeypatch, db[0]) as api:
        assert (await api.get('/api/admin/backups')).status_code == 401
        assert (await api.post(f'/api/admin/backups/{identifier}/download')).status_code == 401
        assert (await api.post(f'/api/admin/backups/{identifier}/download', headers={'X-Admin-Key':'wrong'})).status_code == 401
        assert s3.downloads == 0


async def test_backup_catalog_has_snapshot_and_verification_dates_and_pagination(db, monkeypatch, tmp_path):
    identifier, _, s3, _ = await fixture(db[0], tmp_path, monkeypatch)
    await db[0].execute("INSERT INTO cloud_backup_receipts(kind,object_key,sha256,byte_count) VALUES('image','image-fixture','sha',123)")
    async with make(monkeypatch, db[0]) as api:
        response = await api.get('/api/admin/backups?limit=1', headers=H)
        listing = response.json()
        assert listing['total'] == 1 and listing['download_enabled'] is True
        item = listing['items'][0]
        assert item['id'] == identifier and item['filename'] == 'biblebot-20261015T024146Z-123.dump'
        assert item['created_at'] == '2026-10-15T02:41:46+00:00' and item['verified_at']
        assert item['size_bytes'] > 30000
        assert 'fixture-secret' not in response.text and 'fixture-access' not in response.text
        assert (await api.get('/api/admin/backups?offset=1', headers=H)).json()['items'] == []
        assert (await api.get('/api/admin/backups?limit=101', headers=H)).status_code == 422
        assert s3.downloads == 0  # Listing never fetches large S3 objects.


async def test_download_is_verified_decrypted_exact_dump_and_temporary_files_are_removed(db, monkeypatch, tmp_path):
    identifier, payload, s3, directories = await fixture(db[0], tmp_path, monkeypatch)
    async with make(monkeypatch, db[0]) as api:
        response = await api.post(f'/api/admin/backups/{identifier}/download', headers=H)
        assert response.status_code == 200 and response.content == payload
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['content-type'] == 'application/octet-stream'
        assert 'biblebot-20261015T024146Z-123.dump' in response.headers['content-disposition']
        assert int(response.headers['content-length']) == len(payload)
        assert s3.downloads == 1 and s3.closed
        assert directories and all(not d.exists() for d in directories)
        assert (await api.post('/api/admin/backups/999999/download', headers=H)).status_code == 404


@pytest.mark.parametrize('failure', ['corrupt', 'wrong-key', 'missing', 'not-pgdmp', 'disk-full'])
async def test_invalid_archives_are_not_sent_or_leaked_and_next_download_can_run(db, monkeypatch, tmp_path, failure):
    identifier, _, s3, directories = await fixture(db[0], tmp_path, monkeypatch,
        payload=b'NOTPGDMP: synthetic' * 2000 if failure == 'not-pgdmp' else None)
    if failure == 'corrupt':
        s3.corrupt = True
    elif failure == 'wrong-key':
        monkeypatch.setenv('BACKUP_ENCRYPTION_KEY', 'ab' * 32)
    elif failure == 'missing':
        s3.missing = True
    elif failure == 'disk-full':
        monkeypatch.setattr(admin_backups.shutil, 'disk_usage', lambda _:SimpleNamespace(free=0))
    async with make(monkeypatch, db[0]) as api:
        for _ in range(2):
            response = await api.post(f'/api/admin/backups/{identifier}/download', headers=H)
            assert response.status_code == (404 if failure == 'missing' else 502)
            assert 'fixture-secret' not in response.text and 'signed.fixture' not in response.text
            assert not response.headers.get('content-disposition')
        assert directories and all(not d.exists() for d in directories)


async def test_unconfigured_cloud_and_invalid_keys_do_not_start_downloads(db, monkeypatch, tmp_path):
    identifier, _, s3, _ = await fixture(db[0], tmp_path, monkeypatch)
    monkeypatch.setenv('BACKUP_ENCRYPTION_KEY', 'invalid')
    async with make(monkeypatch, db[0]) as api:
        assert (await api.get('/api/admin/backups', headers=H)).json()['download_enabled'] is False
        assert (await api.post(f'/api/admin/backups/{identifier}/download', headers=H)).status_code == 503
        assert (await api.post('/api/admin/backups/99999999999999999999999/download', headers=H)).status_code == 422
        assert s3.downloads == 0


async def test_download_does_not_block_catalog_and_rejects_concurrent_archives(db, monkeypatch, tmp_path):
    identifier, payload, _, directories = await fixture(db[0], tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    original = admin_backups.prepare

    def blocked(*args):
        started.set()
        assert release.wait(30)
        return original(*args)

    monkeypatch.setattr(admin_backups, 'prepare', blocked)
    async with make(monkeypatch, db[0]) as api:
        task = asyncio.create_task(api.post(f'/api/admin/backups/{identifier}/download', headers=H))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            assert (await api.get('/api/admin/backups', headers=H)).status_code == 200
            assert (await api.post(f'/api/admin/backups/{identifier}/download', headers=H)).status_code == 409
        finally:
            release.set()
        response = await task
        assert response.status_code == 200 and response.content == payload
        assert all(not d.exists() for d in directories)


async def test_non_database_receipts_and_traversal_never_reach_s3(db, monkeypatch, tmp_path):
    identifier, _, s3, directories = await fixture(db[0], tmp_path, monkeypatch)
    async with make(monkeypatch, db[0]) as api:
        await db[0].execute("UPDATE cloud_backup_receipts SET object_key='bible-messenger/v1/database/../../secret.aesgcm' WHERE id=$1", identifier)
        assert (await api.post(f'/api/admin/backups/{identifier}/download', headers=H)).status_code == 502
        await db[0].execute("UPDATE cloud_backup_receipts SET kind='image' WHERE id=$1", identifier)
        assert (await api.post(f'/api/admin/backups/{identifier}/download', headers=H)).status_code == 404
        assert s3.downloads == 0 and all(not d.exists() for d in directories)
