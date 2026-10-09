"""Webhook authentication, body bounds and commit-before-ack behavior."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.maxbot.config import MaxSettings
from app.maxbot.web import create_app
from tests.test_max_events import fixture_event

SECRET='fixture-max-webhook-secret-only'


class Connection:
    def __init__(self):
        self.fetchval=AsyncMock(return_value=1)
        self.commits=0

    @asynccontextmanager
    async def transaction(self):
        yield
        self.commits+=1


class Pool:
    def __init__(self,connection):
        self.connection=connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def web():
    connection=Connection()
    app=create_app(config=MaxSettings(token='fixture-token-only',webhook_secret=SECRET),pool=Pool(connection))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client,connection=connection)


def test_authenticated_webhook_persists_then_acknowledges(web):
    response=web.client.post('/max/webhook',json=fixture_event(),headers={'X-Max-Bot-Api-Secret':SECRET})
    assert response.status_code==200 and response.json()=={'success':True}
    assert web.connection.commits==1
    web.connection.fetchval.assert_awaited_once()


@pytest.mark.parametrize('header',[None,'wrong'])
def test_missing_or_wrong_header_never_reaches_storage(web,header):
    response=web.client.post('/max/webhook',json=fixture_event(),headers={'X-Max-Bot-Api-Secret':header} if header else {})
    assert response.status_code==401
    web.connection.fetchval.assert_not_awaited()


@pytest.mark.parametrize(('payload','status'),[(b'x'*65537,413),(b'{}',400),(b'[]',400)],ids=['oversize','empty-object','array'])
def test_oversize_or_invalid_body_is_not_acknowledged(web,payload,status):
    response=web.client.post('/max/webhook',content=payload,headers={'X-Max-Bot-Api-Secret':SECRET})
    assert response.status_code==status
    web.connection.fetchval.assert_not_awaited()


def test_storage_failure_returns_5xx_for_provider_redelivery():
    connection=Connection()
    connection.fetchval.side_effect=RuntimeError('simulated database outage')
    app=create_app(config=MaxSettings(token='fixture-token-only',webhook_secret=SECRET),pool=Pool(connection))
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.post('/max/webhook',json=fixture_event(),headers={'X-Max-Bot-Api-Secret':SECRET})
    assert response.status_code==500 and connection.commits==0


def test_unconfigured_receiver_never_accepts_real_events():
    connection=Connection()
    with TestClient(create_app(config=MaxSettings(),pool=Pool(connection))) as client:
        assert client.get('/health').status_code==200
        assert client.get('/ready').status_code==503
        assert client.post('/max/webhook',json=fixture_event()).status_code==503
    connection.fetchval.assert_not_awaited()
