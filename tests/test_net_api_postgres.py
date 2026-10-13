"""Панель: прокси и маршруты сервисов (хранение, шифрование, применение к роутеру)."""
import os

import httpx
import pytest

from app.net import store
from app.net.router import ROUTER
from tests.test_admin_api_postgres import H, make
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]
KEY = 'k' * 32


@pytest.fixture(autouse=True)
def _clean_router():
    ROUTER.set_policies({})
    ROUTER.reset()
    yield
    ROUTER.set_policies({})
    ROUTER.reset()


async def test_proxy_crud_hides_credentials_and_validates(db, monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', KEY)
    c = db[0]
    async with make(monkeypatch, c) as client:
        created = await client.post('/api/admin/net/proxies', headers=H, json={'name': 'Nl-1', 'scheme': 'socks5', 'host': '10.1.1.1', 'port': 1080, 'user': 'u', 'password': 'secret-pw'})
        assert created.status_code == 201 and 'secret-pw' not in created.text and created.json()['host'] == '10.1.1.1:1080'
        assert 'secret-pw' not in await c.fetchval('SELECT url_enc FROM net_proxies')
        assert (await client.post('/api/admin/net/proxies', headers=H, json={'name': 'nl-1', 'url': 'http://h:3128'})).status_code == 409
        for bad in ({'name': 'x1', 'url': 'ftp://h:1'}, {'name': 'x2', 'url': 'http://h'}, {'name': 'x3'}, {'name': 'bad name!', 'url': 'http://h:1'}):
            assert (await client.post('/api/admin/net/proxies', headers=H, json=bad)).status_code == 422
        pid = created.json()['id']
        assert (await client.patch(f'/api/admin/net/proxies/{pid}', headers=H, json={'is_enabled': False})).json()['is_enabled'] is False
        assert (await client.patch(f'/api/admin/net/proxies/{pid}', headers=H, json={})).status_code == 422
        assert (await client.delete(f'/api/admin/net/proxies/{pid}', headers=H)).status_code == 200
        assert (await client.delete(f'/api/admin/net/proxies/{pid}', headers=H)).status_code == 404


async def test_policy_is_saved_validated_and_applied_to_router(db, monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', KEY)
    c = db[0]
    async with make(monkeypatch, c) as client:
        p1 = (await client.post('/api/admin/net/proxies', headers=H, json={'name': 'A', 'url': 'http://a:3128'})).json()['id']
        p2 = (await client.post('/api/admin/net/proxies', headers=H, json={'name': 'B', 'url': 'socks5://b:1080'})).json()['id']
        ok = await client.put('/api/admin/net/services/telegram', headers=H, json={
            'strategy': 'auto', 'attempts': 4, 'routes': [{'proxy_id': p1}, {'proxy_id': None}, {'proxy_id': p2, 'enabled': False}]})
        assert ok.status_code == 200
        assert [r.key for r in ROUTER.plan('telegram')][:2] in ([f'proxy:{p1}', 'direct'], ['direct', f'proxy:{p1}'])    # выключенный маршрут не используется
        assert f'proxy:{p2}' not in [r.key for r in ROUTER.policy('telegram').routes]
        assert ROUTER.policy('telegram').strategy == 'auto' and ROUTER.policy('telegram').attempts == 4
        assert (await client.put('/api/admin/net/services/nope', headers=H, json={'routes': []})).status_code == 404
        assert (await client.put('/api/admin/net/services/vk', headers=H, json={'routes': [{'proxy_id': p1}, {'proxy_id': p1}]})).status_code == 422
        assert (await client.put('/api/admin/net/services/vk', headers=H, json={'routes': [{'proxy_id': 99999}]})).status_code == 422
        overview = (await client.get('/api/admin/net', headers=H)).json()
        telegram = next(s for s in overview['services'] if s['key'] == 'telegram')
        assert telegram['strategy'] == 'auto' and [r['name'] for r in telegram['routes']] == ['A', 'Напрямую', 'B']
        assert 'http://a:3128' not in str(overview)
        await client.delete(f'/api/admin/net/proxies/{p1}', headers=H)                      # удаление прокси убирает его из маршрутов
        await store.refresh(c)
        assert f'proxy:{p1}' not in [r.key for r in ROUTER.policy('telegram').routes]


async def test_probe_and_test_buttons_report_reachability(db, monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', KEY)
    c = db[0]
    real = httpx.AsyncHTTPTransport

    def fake_transport(*args, proxy=None, **kwargs):
        def handler(request):
            if proxy is not None and 'dead' in str(proxy.url):
                raise httpx.ConnectError('down')
            return httpx.Response(404)                                                        # любой HTTP-ответ — путь работает
        return httpx.MockTransport(handler)
    async with make(monkeypatch, c) as client:
        good = (await client.post('/api/admin/net/proxies', headers=H, json={'name': 'Good', 'url': 'http://good:3128'})).json()['id']
        dead = (await client.post('/api/admin/net/proxies', headers=H, json={'name': 'Dead', 'url': 'http://dead:3128'})).json()['id']
        monkeypatch.setattr(httpx, 'AsyncHTTPTransport', fake_transport)
        assert (await client.post(f'/api/admin/net/proxies/{good}/test', headers=H, json={'service': 'vk'})).json()['ok'] is True
        broken = (await client.post(f'/api/admin/net/proxies/{dead}/test', headers=H, json={'service': 'vk'})).json()
        assert broken['ok'] is False and broken['error'] == 'ConnectError'
        assert await c.fetchval('SELECT last_test_ok FROM net_proxies WHERE id=$1', dead) is False
        assert (await client.post(f'/api/admin/net/proxies/{good}/test', headers=H, json={'service': 'bad'})).status_code == 422
        await client.put('/api/admin/net/services/youtube', headers=H, json={'routes': [{'proxy_id': dead}, {'proxy_id': good}, {'proxy_id': None}]})
        results = (await client.post('/api/admin/net/services/youtube/probe', headers=H)).json()
        assert [(r['label'], r['ok']) for r in results] == [('Dead', False), ('Good', True), ('Напрямую', True)]
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', real)


async def test_health_snapshot_is_saved_for_panel(db):
    c = db[0]
    ROUTER.record_success('vk', store.DIRECT, 120)
    ROUTER.record_failure('vk', store.Route('proxy:7', 'x', 'http://x:1'), 'ConnectError')
    await store.flush_health(c)
    rows = {r['route']: r for r in await c.fetch("SELECT * FROM net_health WHERE service='vk'")}
    assert rows['direct']['successes'] == 1 and rows['proxy:7']['failures'] == 1 and rows['proxy:7']['last_error'] == 'ConnectError'
