"""Дополнительные генераторы картинок: адаптеры (без сети), защита адресов, порядок и лимиты в маршрутизаторе."""
import base64
import json
import os
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app.services import free_image_providers as free
from app.services import image_router as router
from app.services import secrets_box
from app.services.image_providers import GenerationError


def png(size=(300, 300)):
    out = BytesIO()
    Image.new('RGB', size, 'green').save(out, format='PNG')
    return out.getvalue()


def spec(adapter, **extra):
    base = dict(adapter=adapter, slug='t-' + adapter[:5], api_key='k' * 20, account_id='a' * 32, model='m/odel', base_url='',
                width=1024, height=768, steps=4, extra={})
    return {**base, **extra}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_cloudflare_json_and_binary_responses():
    seen = {}

    def handler(request):
        seen['url'], seen['auth'], seen['body'] = str(request.url), request.headers['authorization'], json.loads(request.content)
        return httpx.Response(200, json={'success': True, 'result': {'image': base64.b64encode(png()).decode()}})
    async with client(handler) as http:
        data, usage, _ = await free.generate(spec('cloudflare', model='@cf/black-forest-labs/flux-1-schnell'), 'a' * 3000, client=http)
    assert data[:4] == b'\x89PNG' and seen['auth'] == 'Bearer ' + 'k' * 20
    assert '/accounts/' + 'a' * 32 + '/ai/run/@cf/black-forest-labs/flux-1-schnell' in seen['url']
    assert len(seen['body']['prompt']) == 2048 and seen['body']['steps'] == 4
    async with client(lambda r: httpx.Response(200, content=png(), headers={'content-type': 'image/png'})) as http:
        assert (await free.generate(spec('cloudflare'), 'x', client=http))[0][:4] == b'\x89PNG'


@pytest.mark.asyncio
async def test_huggingface_pollinations_and_openai_compatible():
    async with client(lambda r: httpx.Response(200, content=png(), headers={'content-type': 'image/png'})) as http:
        assert (await free.generate(spec('huggingface'), 'p', client=http))[0]
        assert (await free.generate(spec('pollinations', api_key=''), 'a lamb/в поле', client=http))[0]
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == 'cdn.example.com':
            return httpx.Response(200, content=png())
        return httpx.Response(200, json={'data': [{'url': 'https://cdn.example.com/x.png'}]})
    async with client(handler) as http:
        data, *_ = await free.generate(spec('openai_compatible', base_url='https://api.example.com/v1'), 'p', client=http)
    assert data[:4] == b'\x89PNG' and calls[0].url.path == '/v1/images/generations'
    assert 'authorization' not in calls[1].headers                          # ключ на сторонний CDN не уходит
    async with client(lambda r: httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})) as http:
        assert (await free.generate(spec('openai_compatible', base_url='https://api.example.com/v1'), 'p', client=http))[0]


@pytest.mark.asyncio
async def test_horde_flow_and_timeout_cancels(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(free.asyncio, 'sleep', instant)
    state = {'polls': 0, 'deleted': False}

    def handler(request):
        path = request.url.path
        if path.endswith('/generate/async'):
            assert request.headers['apikey'] == free.ANON_HORDE_KEY
            return httpx.Response(202, json={'id': 'abcdef12-3456-7890-abcd-ef1234567890'})
        if '/generate/check/' in path:
            state['polls'] += 1
            return httpx.Response(200, json={'done': state['polls'] >= 2})
        if '/generate/status/' in path and request.method == 'DELETE':
            state['deleted'] = True
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'generations': [{'img': base64.b64encode(png()).decode()}]})
    async with client(handler) as http:
        data, *_ = await free.generate(spec('horde', api_key=''), 'p', client=http)
    assert data[:4] == b'\x89PNG' and state['polls'] == 2
    state.update(polls=-10**6)                                              # никогда не готово
    async with client(handler) as http:
        with pytest.raises(GenerationError) as info:
            await free.generate(spec('horde', api_key='', extra={'timeout': 0.01}), 'p', client=http)
    assert info.value.code == 'remote_timeout' and info.value.retry and state['deleted']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401, 'auth'), (402, 'quota'), (429, 'rate_limited'), (503, 'server_uncertain'), (400, 'rejected')])
async def test_error_mapping(status, code):
    async with client(lambda r: httpx.Response(status, json={'error': 'x'})) as http:
        with pytest.raises(GenerationError) as info:
            await free.generate(spec('huggingface'), 'p', client=http)
    assert info.value.code == code


@pytest.mark.asyncio
async def test_missing_key_junk_and_transport_errors():
    with pytest.raises(GenerationError) as info:
        await free.generate(spec('huggingface', api_key=''), 'p')
    assert info.value.code == 'missing_key'
    async with client(lambda r: httpx.Response(200, content=b'<html>not image</html>')) as http:
        with pytest.raises(GenerationError) as info:
            await free.generate(spec('huggingface'), 'p', client=http)
    assert info.value.code == 'response_invalid'

    def boom(request):
        raise httpx.ConnectError('down')
    async with client(boom) as http:
        with pytest.raises(GenerationError) as info:
            await free.generate(spec('huggingface'), 'p', client=http)
    assert info.value.code == 'transport_error' and info.value.retry


def test_validate_spec_blocks_unsafe_addresses():
    ok = dict(adapter='openai_compatible', slug='good-one', base_url='https://api.example.com/v1', model='m')
    free.validate_spec(ok)
    for bad in ({'base_url': 'http://api.example.com'}, {'base_url': 'https://127.0.0.1/v1'}, {'base_url': 'https://10.0.0.5/v1'},
                {'base_url': 'https://localhost/v1'}, {'slug': 'Bad Slug'}, {'adapter': 'nope'}, {'model': ''}, {'base_url': ''}):
        with pytest.raises(ValueError):
            free.validate_spec({**ok, **bad})
    with pytest.raises(ValueError):
        free.validate_spec(dict(adapter='cloudflare', slug='cf', model='m', account_id='short'))


def test_secrets_roundtrip_and_wrong_key(monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY', 'a' * 32)
    monkeypatch.delenv('SECRETS_KEY', raising=False)
    token = secrets_box.encrypt('sk-secret')
    assert 'sk-secret' not in token and secrets_box.decrypt(token) == 'sk-secret' and secrets_box.encrypt('') == ''
    monkeypatch.setenv('ADMIN_API_KEY', 'b' * 32)
    with pytest.raises(ValueError):
        secrets_box.decrypt(token)
    monkeypatch.setenv('ADMIN_API_KEY', 'short')
    with pytest.raises(RuntimeError):
        secrets_box.encrypt('x')


# ----- маршрутизатор (реальная БД) -----------------------------------------------------------------------------

db_required = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')


async def add_provider(c, slug, priority=500, daily=20, enabled=True):
    await c.execute('''INSERT INTO image_custom_providers(slug,adapter,model,api_key_enc,priority,daily_limit,is_enabled,billing_mode)
        VALUES($1,'pollinations','flux','',$2,$3,$4,'free_only')''', slug, priority, daily, enabled)


@db_required
@pytest.mark.asyncio
async def test_custom_providers_go_first_respect_limits_and_cost_nothing(db, monkeypatch):
    from tests.test_image_router_postgres import config, job
    monkeypatch.setenv('ADMIN_API_KEY', 'a' * 32)
    c, _, identifier = await job(db)
    await add_provider(c, 'free-a', 100, daily=1)
    await add_provider(c, 'free-b', 200)
    await add_provider(c, 'off', 1, enabled=False)
    settings = config()
    await router.load_custom(c)
    names = [p.name for p in router.providers(settings)]
    assert 'x-off' not in names and names[-2:] == ['x-free-a', 'x-free-b']
    attempt, provider, reason = await router.reserve(c, identifier, settings)
    assert provider.name == 'x-free-a' and await c.fetchval('SELECT reserved_usd FROM image_generation_attempts WHERE id=$1', attempt) == 0
    await c.execute("UPDATE image_generation_jobs SET state='retry',retry_at=NULL")
    attempt, provider, _ = await router.reserve(c, identifier, settings)
    assert provider.name == 'x-free-b'                                        # free-a уже пробовался в этом задании


@db_required
@pytest.mark.asyncio
async def test_process_job_uses_custom_provider_end_to_end(db, monkeypatch):
    from tests.test_image_router_postgres import job
    from app.services import artwork, free_image_providers, image_quality, visual_scene
    monkeypatch.setenv('ADMIN_API_KEY', 'a' * 32)
    c, _, identifier = await job(db)
    await add_provider(c, 'free-a')
    monkeypatch.setattr(visual_scene, 'describe', AsyncMock(return_value='A shepherd on a quiet hill at dawn'))
    monkeypatch.setattr(image_quality, 'gate', AsyncMock(return_value='approved'))
    sent = {}

    async def fake(spec_, prompt, **_):
        sent['prompt'], sent['slug'] = prompt, spec_['slug']
        return png((600, 400)), {'adapter': 'x'}, None
    monkeypatch.setattr(free_image_providers, 'generate', fake)
    settings = artwork.ArtSettings(provider='manual', provider_order=())          # ручной режим: платных ключей нет
    assert await router.process_job(c, identifier, settings) == 'ready'
    assert sent == {'prompt': 'A shepherd on a quiet hill at dawn', 'slug': 'free-a'}   # в сервис уходит описание сцены, не текст стиха
    assert await c.fetchval("SELECT provider FROM image_generation_attempts") == 'x-free-a'


@db_required
@pytest.mark.asyncio
async def test_failed_scene_planning_never_sends_verse_text_to_free_services(db, monkeypatch):
    from tests.test_image_router_postgres import job
    from app.services import artwork, free_image_providers, visual_scene
    c, _, identifier = await job(db)
    await add_provider(c, 'free-a')
    monkeypatch.setattr(visual_scene, 'describe', AsyncMock(side_effect=ValueError('planner down')))
    guard = AsyncMock()
    monkeypatch.setattr(free_image_providers, 'generate', guard)
    result = await router.process_job(c, identifier, artwork.ArtSettings(provider='manual', provider_order=()))
    assert result in {'providers_unavailable', 'budget_or_not_due'} and not guard.await_count


from tests.test_postgres_integration import db as db  # noqa: E402,F401
