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


# ----- Постраничные клавиатуры VK ----------------------------------------------------------------------------

def rows_of(n_rows, per_row=3, footer=True, audio=False):
    rows = [[_button(f'B{r}{c}', f'v1:x{r}{c}') for c in range(per_row)] for r in range(n_rows)]
    if footer:
        rows.append([_button('◀ Назад', 'v1:settings')])
    if audio:
        rows.append([_button('🔊 Слушать', 'vkaudio:1:2')])
    return rows


def test_paginate_never_loses_buttons_and_keeps_back_and_audio_on_every_page():
    from app.vkbot.ui import paginate
    rows = rows_of(12, audio=True)                         # 36 кнопок + «Назад» + «Слушать»
    pages = paginate(rows)
    assert len(pages) > 1
    seen = set()
    for number, page in enumerate(pages):
        assert len(page) <= 6 and sum(map(len, page)) <= 10
        labels = [b['action']['label'] for row in page for b in row]
        assert '◀ Назад' in labels and '🔊 Слушать' in labels
        assert f'{number + 1}/{len(pages)}' in labels
        seen |= {label for label in labels if label.startswith('B')}
    assert seen == {f'B{r}{c}' for r in range(12) for c in range(3)}      # достижимы все 36 кнопок


def test_small_keyboard_is_single_page_without_navigation():
    from app.vkbot.ui import paginate
    pages = paginate(rows_of(2))
    assert len(pages) == 1 and all('vkpg' not in b['action'].get('payload', '') for row in pages[0] for b in row)


def test_navigation_payloads_wrap_around():
    from app.vkbot.ui import paginate
    pages = paginate(rows_of(10))
    nav = [row for row in pages[0] if row[0]['action']['payload'].startswith('{"d":"vkpg:')][0]
    assert json.loads(nav[0]['action']['payload'])['d'] == f'vkpg:{len(pages) - 1}'
    assert json.loads(nav[2]['action']['payload'])['d'] == 'vkpg:1'


def test_wide_row_is_split_not_dropped():
    from app.vkbot.ui import paginate
    wide = [[_button(f'W{i}', f'v1:w{i}') for i in range(5)]] + rows_of(8)
    labels = {b['action']['label'] for page in paginate(wide) for row in page for b in row}
    assert {f'W{i}' for i in range(5)} <= labels


@pytest.mark.asyncio
async def test_voice_upload_uses_audio_message_type_and_parses_response():
    seen = []

    def handler(request):
        if request.url.host == 'pu.vk.com':
            return httpx.Response(200, json={'file': 'F'})
        form = {k: v for k, v in (x.split('=') for x in request.content.decode().split('&'))}
        seen.append((request.url.path.rsplit('/', 1)[-1], form.get('type')))
        if request.url.path.endswith('getMessagesUploadServer'):
            return httpx.Response(200, json={'response': {'upload_url': 'https://pu.vk.com/up'}})
        return httpx.Response(200, json={'response': {'type': 'audio_message', 'audio_message': {'id': 9, 'owner_id': -5, 'access_key': 'k'}}})
    vk = client(handler)
    assert await vk.upload('voice', 1, b'ogg', 'v.ogg', 'audio/ogg') == 'doc-5_9_k'
    assert ('docs.getMessagesUploadServer', 'audio_message') in seen
    await vk.close()


@pytest.mark.asyncio
async def test_reply_uses_forward_with_conversation_message_id():
    bodies = []

    def handler(request):
        bodies.append(request.content.decode())
        return httpx.Response(200, json={'response': [{'peer_id': 5, 'conversation_message_id': 3}]})
    vk = client(handler)
    await vk.send(5, message='x', reply_to_cmid=41)
    from urllib.parse import unquote
    assert 'conversation_message_ids' in unquote(bodies[0]) and 'reply_to=' not in bodies[0]
    await vk.close()


@pytest.mark.parametrize('code', [900, 912])
def test_blocked_user_codes_are_forbidden(code):
    assert VkAPIError(code).send_error().kind == 'forbidden'
