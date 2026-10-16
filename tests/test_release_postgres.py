"""Проверить настоящие heartbeat в изолированной БД, не смешивая успешную установку с текущим состоянием бота."""
import os

import pytest

from app.web import api_release
from tests.test_admin_api_postgres import H, make
from tests.test_postgres_integration import db as db
from tests.test_release_info import files, snapshot, SHA

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


async def test_deployment_health_and_current_signals_are_independent(db, monkeypatch, tmp_path):
    files(monkeypatch, tmp_path, snapshot())
    async def offline_github():
        return {'available':False,'run':None,'checked_at':None,'stale':False}
    monkeypatch.setattr(api_release.github, 'get', offline_github)
    conn = db[0]
    await conn.execute("""INSERT INTO service_heartbeats(service,last_seen) VALUES
        ('bot',now()),('maxbot',now()-interval '181 seconds'),('vkbot',now()+interval '1 hour')
        ON CONFLICT(service) DO UPDATE SET last_seen=excluded.last_seen""")
    async with make(monkeypatch, conn) as client:
        assert (await client.get('/api/admin/release')).status_code == 401
        response = await client.get('/api/admin/release', headers=H)
    assert response.status_code == 200
    data = response.json()
    assert data['build']['commit']==SHA
    assert data['bots'][0]['verified'] and data['bots'][0]['live']
    assert data['bots'][1]['verified'] and not data['bots'][1]['live']
    assert not data['bots'][2]['expected'] and not data['bots'][2]['live']
    assert not data['github']['available']


async def test_unconfirmed_new_image_is_not_reported_as_deployed(db, monkeypatch, tmp_path):
    files(monkeypatch, tmp_path, snapshot('b'*40), snapshot(SHA,'failed'))
    async def offline_github():
        return {'available':False,'run':None,'checked_at':None,'stale':False}
    monkeypatch.setattr(api_release.github, 'get', offline_github)
    async with make(monkeypatch, db[0]) as client:
        data = (await client.get('/api/admin/release',headers=H)).json()
    assert data['server']['last_attempt']['status']=='failed'
    assert all(not bot['verified'] for bot in data['bots'])
