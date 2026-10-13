from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi.testclient import TestClient

from app.services.metrics import Exposition
from app.web import main as web

KEY = 'k' * 32


class Pool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


def client(monkeypatch):
    calls = []

    async def fake_pool(_settings):
        return Pool()

    async def fake_collect(_connection, _budget):
        calls.append(1)
        e = Exposition()
        e.add('bible_test', 'h', len(calls))
        return e

    monkeypatch.setattr(web, 'create_pool', fake_pool)
    monkeypatch.setattr(web, 'collect_metrics', fake_collect)
    monkeypatch.setattr(web, 'settings', replace(web.settings, admin_api_key=KEY))
    web._metrics_cache[:] = [0.0, '']
    return TestClient(web.app), calls


def test_metrics_requires_basic_auth(monkeypatch):
    c, _ = client(monkeypatch)
    assert c.get('/metrics').status_code == 401
    assert c.get('/metrics', auth=('admin', 'wrong')).status_code == 401


def test_metrics_served_and_cached(monkeypatch):
    c, calls = client(monkeypatch)
    first = c.get('/metrics', auth=('admin', KEY))
    assert first.status_code == 200 and 'bible_test 1' in first.text
    assert first.headers['content-type'].startswith('text/plain')
    assert 'bible_test 1' in c.get('/metrics', auth=('admin', KEY)).text and len(calls) == 1
