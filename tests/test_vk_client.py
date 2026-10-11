"""Клиент и вспомогательные модули ВКонтакте: ошибки API, клавиатуры, события — без сети."""
import json

import httpx
import pytest

from app.services.errors import SendError
from app.vkbot.client import VkAPIError, VkClient
from app.vkbot.config import VkSettings
from app.vkbot.events import parse_event
from app.vkbot.ui import _button, fit, to_text


def client(handler):
    return VkClient(VkSettings(token='t' * 30, group_id=1), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_send_returns_conversation_message_id():
    seen = {}

    def handler(request):
        seen.update(dict(x.split('=') for x in request.content.decode().split('&')))
        return httpx.Response(200, json={'response': [{'peer_id': 5, 'conversation_message_id': 77}]})

    vk = client(handler)
    assert await vk.send(5, message='hi') == 77
    assert seen['v'] == '5.199' and 'access_token' in seen
    await vk.close()


@pytest.mark.parametrize('code,kind', [(901, 'forbidden'), (917, 'forbidden'), (6, 'retry'), (5, 'retry'), (100, 'rejected')])
def test_error_mapping(code, kind):
    assert VkAPIError(code).send_error().kind == kind


def test_network_error_is_uncertain_for_send_but_retry_for_edit():
    assert VkAPIError(0).send_error().kind == 'uncertain'
    assert VkAPIError(0).send_error(editing=True).kind == 'retry'
    assert isinstance(VkAPIError(0).send_error(), SendError)


@pytest.mark.asyncio
async def test_api_error_and_bad_method():
    vk = client(lambda r: httpx.Response(200, json={'error': {'error_code': 901, 'error_msg': 'denied'}}))
    with pytest.raises(VkAPIError) as info:
        await vk.call('messages.send')
    assert info.value.code == 901
    with pytest.raises(ValueError):
        await vk.call('bad method!')
    await vk.close()


@pytest.mark.asyncio
async def test_poll_refuses_foreign_host():
    vk = client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(VkAPIError):
        await vk.poll({'server': 'https://evil.example/x', 'key': 'k', 'ts': '1'})
    await vk.close()


def test_fit_limits_rows_and_buttons():
    rows = [[_button(str(i), f'd{i}') for i in range(5)] for _ in range(8)]
    out = fit(rows)
    assert len(out) <= 6 and sum(map(len, out)) <= 10
    audio = [_button('🔊', 'vkaudio:1:2')]
    out = fit(rows + [audio])
    assert out[-1] == audio and sum(map(len, out)) <= 10


def test_to_text_strips_markup():
    assert to_text('<b>Привет</b> &amp; мир') == 'Привет & мир'
    assert 'https://x.example' in to_text('<a href="https://x.example">сайт</a>')


def test_event_validation_and_dedup_key():
    update = {'type': 'message_new', 'event_id': 'abc', 'object': {'message': {'peer_id': 2000000001, 'text': 'x'}}}
    key, chat, _ = parse_event(update)
    assert chat == '2000000001' and key == parse_event(json.loads(json.dumps(update)))[0]
    for bad in ({}, {'type': 'x', 'object': {}}, {**update, 'event_id': ''}, {**update, 'object': {'message': {'peer_id': 'a'}}}):
        with pytest.raises(ValueError):
            parse_event(bad)
