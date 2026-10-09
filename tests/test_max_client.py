"""MAX wire protocol, mutation uncertainty, media upload and secret separation."""
from __future__ import annotations

import json

import httpx
import pytest

from app.maxbot.client import MaxAPIError, MaxClient, validate_body
from app.maxbot.config import MaxSettings


@pytest.mark.asyncio
async def test_send_exact_destination_and_authorization_header():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == 'platform-api2.max.ru'
        assert request.headers['authorization'] == 'fixture-token-only'
        assert 'access_token' not in request.url.params
        assert dict(request.url.params) == {'user_id': '123'}
        assert json.loads(request.content) == {'text': 'Verse', 'format': 'html'}
        return httpx.Response(200, json={'message': {'body': {'mid': 'mid.fixture.123'}}})

    client = MaxClient(MaxSettings(token='fixture-token-only'), transport=httpx.MockTransport(handle))
    try:
        assert await client.send(user_id=123, body={'text': 'Verse', 'format': 'html'}) == 'mid.fixture.123'
        assert len(calls) == 1
        with pytest.raises(ValueError):
            await client.send(user_id=123, chat_id=456, body={'text': 'Verse'})
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'code', 'kind'), [
    (429, 'rate.limit', 'retry'), (400, 'attachment.not.ready', 'retry'),
    (403, 'access.denied', 'forbidden'), (401, 'auth', 'forbidden'),
    (400, 'invalid', 'rejected'), (503, 'unavailable', 'uncertain'),
])
async def test_api_refusals_are_not_blindly_retried(status, code, kind):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={'code': code, 'message': 'secret must not be logged'},
                              headers={'Retry-After': '12'})

    client = MaxClient(MaxSettings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(MaxAPIError) as caught:
            await client.send(chat_id=42, body={'text': 'Verse'})
        assert caught.value.send_error().kind == kind
        assert 'secret' not in str(caught.value)
        assert len(calls) == 1
        if status == 503:
            assert caught.value.send_error(editing=True).kind == 'retry'
        if kind == 'retry':
            assert caught.value.send_error().retry_after == 12
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [httpx.Response(200, content=b'not-json'),
                                    httpx.Response(200, json={'message': {'body': {'mid': 7}}})])
async def test_success_without_message_ack_is_uncertain(response):
    client = MaxClient(MaxSettings(), transport=httpx.MockTransport(lambda _: response))
    try:
        with pytest.raises(MaxAPIError) as caught:
            await client.send(chat_id=42, body={'text': 'Verse'})
        assert caught.value.send_error().kind == 'uncertain'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_timeout_does_not_send_again():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('private diagnostic')

    client = MaxClient(MaxSettings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(MaxAPIError) as caught:
            await client.send(chat_id=42, body={'text': 'Verse'})
        assert caught.value.send_error().kind == 'uncertain'
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize('body', [
    {'text': 'x' * 4001}, {}, {'text': 42},
    {'attachments': [{'type': 'audio'}, {'type': 'inline_keyboard'}]},
    {'attachments': [{'type': 'audio'}, {'type': 'image'}]},
])
def test_invalid_bodies_rejected_locally(body):
    with pytest.raises(ValueError):
        validate_body(body)


def test_image_and_buttons_valid_audio_separate():
    validate_body({'text': 'x' * 4000,
                   'attachments': [{'type': 'image'}, {'type': 'inline_keyboard'}]})
    validate_body({'attachments': [{'type': 'audio'}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['image', 'audio'])
async def test_upload_protocol_does_not_leak_authorization(kind):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == '/uploads':
            assert request.url.params['type'] == kind
            host = 'iu.oneme.ru' if kind == 'image' else 'omu.okcdn.ru'
            return httpx.Response(200, json={'url': f'https://{host}/upload?sig=fixture', 'token': 'audio-fixture'})
        assert 'authorization' not in request.headers
        assert b'name="data"' in request.content
        assert b'fixture-bytes' in request.content
        return (httpx.Response(200, json={'photos': {'123': {'token': 'image-fixture'}}})
                if kind == 'image' else httpx.Response(200, content=b'<retval>1</retval>'))

    client = MaxClient(MaxSettings(token='private-fixture-token'), transport=httpx.MockTransport(handle))
    try:
        payload = await client.upload(kind, b'fixture-bytes', 'fixture.mp3', 'audio/mpeg')
        assert payload == ({'photos': {'123': {'token': 'image-fixture'}}} if kind == 'image'
                           else {'token': 'audio-fixture'})
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://iu.oneme.ru/upload', 'https://127.0.0.1/upload',
                               'https://iu.oneme.ru.evil.test/upload', 'https://user@iu.oneme.ru/upload',
                               'https://iu.oneme.ru:8443/upload'])
async def test_untrusted_upload_endpoint_never_receives_bytes(url):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'url': url})

    client = MaxClient(MaxSettings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(MaxAPIError):
            await client.upload('image', b'fixture', 'image.png', 'image/png')
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_menu_uses_commands_response_not_simple_success():
    commands = [{'name': 'random', 'description': 'Random verse'}]

    def handle(request):
        assert request.method == 'PATCH' and request.url.path == '/me/commands'
        assert json.loads(request.content) == {'commands': commands}
        return httpx.Response(200, json={'commands': commands})

    client = MaxClient(MaxSettings(), transport=httpx.MockTransport(handle))
    try:
        await client.commands(commands)
    finally:
        await client.close()


def test_config_optional_and_secret_repr(monkeypatch):
    for key in ['MAX_BOT_TOKEN', 'MAX_WEBHOOK_SECRET', 'MAX_WEBHOOK_URL', 'MAX_CA_FILE']:
        monkeypatch.delenv(key, raising=False)
    assert MaxSettings.from_env().token == ''
    with pytest.raises(RuntimeError):
        MaxSettings.from_env(require_token=True)
    monkeypatch.setenv('MAX_BOT_TOKEN', 'private-fixture-token')
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'private-fixture-webhook-secret')
    assert 'private-fixture' not in repr(MaxSettings.from_env())
    monkeypatch.setenv('MAX_WEBHOOK_URL', 'https://example.test:443/max/webhook')
    with pytest.raises(ValueError):
        MaxSettings.from_env()
