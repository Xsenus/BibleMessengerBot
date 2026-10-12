"""Панель: генераторы картинок, настройки, чаты, пользователи, доставки и правка объявлений."""
import base64
import os
from io import BytesIO

import pytest
from PIL import Image

from app.services import features, free_image_providers
from tests.test_admin_api_postgres import H, make, prepare
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]
KEY = 'k' * 32


def png():
    out = BytesIO()
    Image.new('RGB', (300, 300), 'red').save(out, format='PNG')
    return out.getvalue()


async def test_image_provider_crud_hides_secrets_and_validates(db, monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', KEY)
    c = db[0]
    async with make(monkeypatch, c) as client:
        listing = (await client.get('/api/admin/image-providers', headers=H)).json()
        assert [b['name'] for b in listing['builtin']][0] == 'openai' and listing['templates'] and listing['secrets_ready']
        assert all(t['howto'] for t in listing['templates']) and 'planner_ready' in listing
        good = {'slug': 'hf-flux', 'adapter': 'huggingface', 'model': 'black-forest-labs/FLUX.1-schnell', 'api_key': 'hf_secret_value_1234'}
        created = await client.post('/api/admin/image-providers', headers=H, json=good)
        assert created.status_code == 201 and created.json()['has_key'] is True and 'secret' not in created.text
        assert (await client.post('/api/admin/image-providers', headers=H, json=good)).status_code == 409
        bad_url = {**good, 'slug': 'evil', 'adapter': 'openai_compatible', 'base_url': 'https://127.0.0.1/v1'}
        assert (await client.post('/api/admin/image-providers', headers=H, json=bad_url)).status_code == 422
        stored = await c.fetchval("SELECT api_key_enc FROM image_custom_providers WHERE slug='hf-flux'")
        assert 'hf_secret' not in stored
        pid = created.json()['id']
        patched = await client.patch(f'/api/admin/image-providers/{pid}', headers=H, json={'priority': 50, 'daily_limit': 5, 'is_enabled': False})
        assert patched.status_code == 200 and patched.json()['priority'] == 50 and patched.json()['has_key']
        cleared = await client.patch(f'/api/admin/image-providers/{pid}', headers=H, json={'clear_key': True})
        assert cleared.json()['has_key'] is False
        assert (await client.patch(f'/api/admin/image-providers/{pid}', headers=H, json={})).status_code == 422
        assert (await client.delete(f'/api/admin/image-providers/{pid}', headers=H)).status_code == 200
        assert (await client.delete(f'/api/admin/image-providers/{pid}', headers=H)).status_code == 404


async def test_provider_test_button_records_result_and_returns_preview(db, monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', KEY)
    c = db[0]

    async def fake(spec, prompt, **_):
        return png(), {}, None
    monkeypatch.setattr(free_image_providers, 'generate', fake)
    async with make(monkeypatch, c) as client:
        pid = (await client.post('/api/admin/image-providers', headers=H, json={'slug': 'poll', 'adapter': 'pollinations'})).json()['id']
        result = (await client.post(f'/api/admin/image-providers/{pid}/test', headers=H)).json()
        assert result['ok'] and base64.b64decode(result['preview'])[:2] == b'\xff\xd8'
        assert await c.fetchval('SELECT last_test_ok FROM image_custom_providers') is True

        async def broken(spec, prompt, **_):
            raise free_image_providers.GenerationError('auth')
        monkeypatch.setattr(free_image_providers, 'generate', broken)
        result = (await client.post(f'/api/admin/image-providers/{pid}/test', headers=H)).json()
        assert result == {**result, 'ok': False, 'error': 'auth'}
        assert await c.fetchval('SELECT last_test_error FROM image_custom_providers') == 'auth'


async def test_secrets_require_encryption_key(db, monkeypatch):
    c = db[0]
    async with make(monkeypatch, c) as client:
        monkeypatch.delenv('SECRETS_KEY', raising=False)
        monkeypatch.setenv('ADMIN_API_KEY', 'short')
        response = await client.post('/api/admin/image-providers', headers=H, json={
            'slug': 'cf', 'adapter': 'huggingface', 'model': 'm', 'api_key': 'token-token-token'})
        assert response.status_code == 409 and response.json()['detail'] == 'secrets_key_missing'
        assert (await client.post('/api/admin/image-providers', headers=H, json={'slug': 'poll', 'adapter': 'pollinations'})).status_code == 201


async def test_support_section_toggle_roundtrip(db, monkeypatch):
    monkeypatch.setenv('SUPPORT_SECTION_ENABLED', 'false')
    c = db[0]
    features._override.clear()
    async with make(monkeypatch, c) as client:
        assert (await client.get('/api/admin/settings', headers=H)).json() == {'support_section': False, 'support_source': 'env'}
        assert (await client.put('/api/admin/settings', headers=H, json={'support_section': True})).status_code == 200
        assert features.support_enabled() is True
        features._override.clear()
        await features.refresh(c)                                           # другой процесс прочитает значение из БД
        assert features.support_enabled() is True
        assert (await client.get('/api/admin/settings', headers=H)).json()['support_source'] == 'panel'
        assert (await client.put('/api/admin/settings', headers=H, json={})).status_code == 422
    features._override.clear()


async def test_chats_users_and_deliveries(db, monkeypatch):
    c = db[0]
    await prepare(c)
    async with make(monkeypatch, c) as client:
        chats = (await client.get('/api/admin/chats', headers=H)).json()
        assert {x['id'] for x in chats} >= {101, 202}
        assert (await client.put('/api/admin/chats/202/active', headers=H, json={'value': False})).status_code == 200
        assert await c.fetchval('SELECT is_active FROM telegram_chats WHERE telegram_chat_id=202') is False
        assert (await client.put('/api/admin/chats/999999/active', headers=H, json={'value': False})).status_code == 404
        users = (await client.get('/api/admin/users', headers=H)).json()
        assert users[0]['id'] == 101 and users[0]['is_owner']
        assert (await client.put('/api/admin/users/202/blocked', headers=H, json={'value': True})).status_code == 200
        assert (await client.put('/api/admin/users/101/blocked', headers=H, json={'value': True})).status_code == 404    # владельца блокировать нельзя
        assert await c.fetchval('SELECT is_blocked FROM telegram_users WHERE telegram_user_id=202') is True
        assert (await client.get('/api/admin/deliveries/attention', headers=H)).json() == []
        assert (await client.post('/api/admin/deliveries/99999/resolve', headers=H, json={'action': 'cancel'})).status_code == 404
        assert (await client.post('/api/admin/deliveries/1/resolve', headers=H, json={'action': 'bad'})).status_code == 422


async def test_announcement_edit_and_image_queue_retry(db, monkeypatch):
    c = db[0]
    await prepare(c)
    async with make(monkeypatch, c) as client:
        created = (await client.post('/api/admin/announcements', headers=H, json={'body': 'Старый', 'kind': 'daily', 'send_time': '10:00:00'})).json()
        aid = created['id']
        assert (await client.patch(f'/api/admin/announcements/{aid}', headers=H, json={'body': 'Новый', 'audience': {'platforms': ['vk']}})).status_code == 200
        row = await c.fetchrow('SELECT body,audience FROM announcements WHERE id=$1', aid)
        assert row['body'] == 'Новый' and 'vk' in row['audience']
        assert (await client.patch(f'/api/admin/announcements/{aid}', headers=H, json={})).status_code == 422
        await client.post(f'/api/admin/announcements/{aid}/status', headers=H, json={'status': 'cancelled'})
        assert (await client.patch(f'/api/admin/announcements/{aid}', headers=H, json={'body': 'x'})).status_code == 404
        assert (await client.get('/api/admin/image-queue', headers=H)).json()['jobs'] == {}
        assert (await client.post('/api/admin/image-queue/retry', headers=H)).json() == {'requeued': 0}
