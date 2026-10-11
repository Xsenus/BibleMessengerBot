"""REST-интерфейс панели администратора: авторизация, объявления, источники, видеоканалы, администраторы."""
import os
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from app.services import chat_sources
from app.web import main as web
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')
KEY = 'k' * 32
H = {'X-Admin-Key': KEY}
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def make(monkeypatch, connection):
    async def pool(_settings=None):
        return Pool(connection)
    monkeypatch.setattr(web, 'settings', replace(web.settings, admin_api_key=KEY))
    monkeypatch.setattr(web, 'create_pool', pool)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://test')


async def prepare(c):
    await create_destination(c, 101)
    await create_destination(c, 202)
    await c.execute("INSERT INTO telegram_users(telegram_user_id,platform,timezone,first_name) VALUES(101,'telegram','UTC','A'),(202,'telegram','UTC','B') ON CONFLICT DO NOTHING")
    await c.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')


@pytest.mark.asyncio
async def test_requires_key(db, monkeypatch):
    async with make(monkeypatch, db[0]) as client:
        assert (await client.get('/api/admin/overview')).status_code == 401
        assert (await client.get('/api/admin/overview', headers={'X-Admin-Key': 'x' * 32})).status_code == 401
        response = await client.get('/api/admin/overview', headers=H)
        assert response.status_code == 200 and 'queue' in response.json()


@pytest.mark.asyncio
async def test_weekly_announcement_lifecycle_and_validation(db, monkeypatch):
    c = db[0]
    await prepare(c)
    async with make(monkeypatch, c) as client:
        bad = await client.post('/api/admin/announcements', headers=H, json={'body': 'x', 'kind': 'weekly'})
        assert bad.status_code == 422
        bad = await client.post('/api/admin/announcements', headers=H, json={'body': 'x', 'kind': 'daily', 'send_time': '10:00', 'timezone': 'Nope/Zone'})
        assert bad.status_code == 422
        created = await client.post('/api/admin/announcements', headers=H, json={
            'title': 'Собрание', 'body': 'В субботу в 10:00', 'kind': 'weekly', 'send_time': '10:00:00',
            'timezone': 'Europe/Moscow', 'days_of_week': [6], 'max_runs': 3, 'audience': {'platforms': ['telegram']}})
        assert created.status_code == 201 and created.json()['days_of_week'] == [6]
        ann = created.json()['id']
        assert (await client.post(f'/api/admin/announcements/{ann}/run', headers=H)).status_code == 200
        assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='announcement'") >= 1
        assert (await client.post(f'/api/admin/announcements/{ann}/status', headers=H, json={'status': 'paused'})).status_code == 200
        assert (await client.post('/api/admin/announcements/99999/status', headers=H, json={'status': 'paused'})).status_code == 404
        listing = (await client.get('/api/admin/announcements', headers=H)).json()
        assert listing[0]['status'] == 'paused'


@pytest.mark.asyncio
async def test_media_upload_rejects_non_images(db, monkeypatch):
    async with make(monkeypatch, db[0]) as client:
        assert (await client.post('/api/admin/media', headers=H, content=b'not an image')).status_code == 415
        ok = await client.post('/api/admin/media', headers=H, content=PNG)
        assert ok.status_code == 201 and ok.json()['asset_id'] > 0


@pytest.mark.asyncio
async def test_sources_and_posts(db, monkeypatch):
    c = db[0]
    await prepare(c)
    source = await chat_sources.ensure_source(c, 'telegram', -100500, 'Канал')
    async with make(monkeypatch, c) as client:
        patched = await client.patch(f"/api/admin/sources/{source['id']}", headers=H,
                                     json={'is_enabled': True, 'mode': 'capture', 'min_length': 1, 'audience': {'chat_types': ['private']}})
        assert patched.status_code == 200
        assert (await client.patch('/api/admin/sources/999999', headers=H, json={'is_enabled': True})).status_code == 404
        assert (await client.patch(f"/api/admin/sources/{source['id']}", headers=H, json={})).status_code == 422
        post = await chat_sources.ingest_post(c, 'telegram', -100500, '1', 'Привет')
        posts = (await client.get('/api/admin/posts', headers=H)).json()
        assert posts[0]['id'] == post
        sent = await client.post(f'/api/admin/posts/{post}/publish', headers=H)
        assert sent.status_code == 200 and sent.json()['queued'] >= 1
        assert (await client.post(f'/api/admin/posts/{post}/publish', headers=H)).status_code == 422   # повтор запрещён


@pytest.mark.asyncio
async def test_video_feeds_crud(db, monkeypatch):
    c = db[0]
    async with make(monkeypatch, c) as client:
        feed = await client.post('/api/admin/videos', headers=H, json={'platform': 'youtube', 'channel': 'UC' + 'a' * 22, 'notify': True})
        assert feed.status_code == 201
        fid = feed.json()['id']
        assert (await client.post('/api/admin/videos', headers=H, json={'platform': 'youtube', 'channel': 'bad'})).status_code == 422
        assert (await client.patch(f'/api/admin/videos/{fid}', headers=H, json={'notify': False, 'interval_minutes': 60})).status_code == 200
        assert (await client.patch(f'/api/admin/videos/{fid}', headers=H, json={'interval_minutes': 1})).status_code == 422
        assert (await client.delete(f'/api/admin/videos/{fid}', headers=H)).status_code == 200
        assert (await client.delete(f'/api/admin/videos/{fid}', headers=H)).status_code == 404


@pytest.mark.asyncio
async def test_admin_invites(db, monkeypatch):
    c = db[0]
    await prepare(c)
    async with make(monkeypatch, c) as client:
        code = (await client.post('/api/admin/admins/invite', headers=H)).json()['code']
        from app.services.accounts import redeem_admin_invite
        assert await redeem_admin_invite(c, 202, code)
        admins = (await client.get('/api/admin/admins', headers=H)).json()
        assert {a['telegram_user_id'] for a in admins} == {101, 202}
        assert (await client.delete('/api/admin/admins/202', headers=H)).status_code == 200
        assert (await client.delete('/api/admin/admins/101', headers=H)).status_code == 404       # владельца снять нельзя
