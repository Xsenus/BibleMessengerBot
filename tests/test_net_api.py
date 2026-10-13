"""Per-platform route capabilities are checked before persisted policies can break a bot."""
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from app.net.router import ROUTER
from app.web.api_net import build_router
from tests.test_postgres_integration import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


def client_for(connection):
    @asynccontextmanager
    async def lease():
        yield connection

    async def pool():
        return SimpleNamespace(acquire=lease)

    app = FastAPI()
    app.include_router(build_router(lambda: None, pool))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture.invalid')


async def test_https_proxy_cannot_be_added_to_active_telegram_policy(db, monkeypatch):
    connection, _, _ = db
    monkeypatch.setenv('ADMIN_API_KEY', 'fixture-not-real-' + 'a' * 32)
    async with client_for(connection) as client:
        response = await client.post('/api/admin/net/proxies', json={'name': 'TLS proxy', 'url': 'https://proxy.invalid:3128'})
        assert response.status_code == 201
        proxy_id = response.json()['id']
        payload = {'strategy': 'failover', 'attempts': 2, 'routes': [{'proxy_id': proxy_id}, {'proxy_id': None}]}
        assert (await client.put('/api/admin/net/services/telegram', json=payload)).status_code == 422
        assert await connection.fetchval("SELECT count(*) FROM net_policies WHERE service='telegram'") == 0
        assert (await client.put('/api/admin/net/services/max', json=payload)).status_code == 200
        probe = await client.post(f'/api/admin/net/proxies/{proxy_id}/test', json={'service': 'telegram'})
        assert probe.status_code == 200 and not probe.json()['ok']
    ROUTER.set_policies({})


async def test_proxy_used_by_telegram_cannot_be_changed_to_unsupported_scheme(db, monkeypatch):
    connection, _, _ = db
    monkeypatch.setenv('ADMIN_API_KEY', 'fixture-not-real-' + 'a' * 32)
    async with client_for(connection) as client:
        response = await client.post('/api/admin/net/proxies', json={'name': 'Remote DNS', 'url': 'socks5h://proxy.invalid:1080'})
        proxy_id = response.json()['id']
        payload = {'strategy': 'failover', 'attempts': 2, 'routes': [{'proxy_id': proxy_id}, {'proxy_id': None}]}
        assert (await client.put('/api/admin/net/services/telegram', json=payload)).status_code == 200
        response = await client.patch(f'/api/admin/net/proxies/{proxy_id}', json={'url': 'https://proxy.invalid:3128'})
        assert response.status_code == 422
        assert await connection.fetchval('SELECT scheme FROM net_proxies WHERE id=$1', proxy_id) == 'socks5h'
    ROUTER.set_policies({})
