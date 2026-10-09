"""Check MAX send/edit outcomes and quarantine before a cached media token is used."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.maxbot import transport
from app.maxbot.client import MaxAPIError
from app.maxbot.transport import MaxSender
from app.services.errors import SendError, UserError


@pytest.fixture
def context(monkeypatch):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={'ui_language': 'ru'}),
                                 fetchval=AsyncMock(return_value=None), execute=AsyncMock())
    client = SimpleNamespace(send=AsyncMock(return_value='mid.fixture'), edit=AsyncMock(),
                             upload=AsyncMock(return_value={'token': 'fixture-token'}))
    monkeypatch.setattr(transport, 'external', AsyncMock(return_value=303))
    monkeypatch.setattr(transport, 'message_external', AsyncMock(return_value='mid.original'))
    monkeypatch.setattr(transport, 'remember_message', AsyncMock(return_value=77))
    monkeypatch.setattr(transport, 'wait_send_slot', AsyncMock())
    return MaxSender(client, 900, connection), client, connection


async def test_send_persists_string_mid_as_local_ack(context):
    sender, client, _ = context
    assert await sender.send(-4000000000000000, 'Fixture') == 77
    client.send.assert_awaited_once_with(chat_id=303, body={'text': 'Fixture', 'format': 'html', 'attachments': []})
    transport.remember_message.assert_awaited_once_with(sender.connection, 900, -4000000000000000, 'mid.fixture')


async def test_fixed_original_edit_never_becomes_replacement_send(context):
    sender, client, _ = context
    assert await sender.send(-4000000000000000,
                             {'kind': 'rich_edit', 'text': 'Fixture edit', 'message_id': 66}) == 66
    client.edit.assert_awaited_once_with('mid.original', {'text': 'Fixture edit', 'format': 'html', 'attachments': []})
    client.send.assert_not_awaited()


async def test_foreign_original_cannot_be_edited(context, monkeypatch):
    sender, client, _ = context
    monkeypatch.setattr(transport, 'message_external', AsyncMock(side_effect=UserError('forbidden')))
    with pytest.raises(SendError) as caught:
        await sender.send(-4000000000000000, {'kind': 'rich_edit', 'text': 'Fixture', 'message_id': 66})
    assert caught.value.kind == 'forbidden'
    client.edit.assert_not_awaited()
    client.send.assert_not_awaited()


@pytest.mark.parametrize(('editing', 'kind'), [(False, 'uncertain'), (True, 'retry')])
async def test_lost_ack_send_uncertain_edit_retry(context, editing, kind):
    sender, client, _ = context
    client.send.side_effect = MaxAPIError(0, 'network')
    client.edit.side_effect = MaxAPIError(0, 'network')
    chunk = {'kind': 'rich_edit', 'text': 'Fixture', 'message_id': 66} if editing else 'Fixture'
    with pytest.raises(SendError) as caught:
        await sender.send(-4000000000000000, chunk)
    assert caught.value.kind == kind
    transport.remember_message.assert_not_awaited()


async def test_quarantined_asset_cannot_be_revived_by_cached_token(context):
    sender, client, connection = context
    connection.fetchrow.return_value = None
    connection.fetchval.return_value = '{"token":"old-cached-token"}'
    with pytest.raises(SendError) as caught:
        await sender.media('image', 123)
    assert caught.value.kind == 'rejected'
    connection.fetchval.assert_not_awaited()
    client.upload.assert_not_awaited()


async def test_approved_media_uses_separate_max_cache(context):
    sender, client, connection = context
    connection.fetchrow.return_value = {'mime_type': 'image/png', 'image_data': b'fixture'}
    connection.fetchval.return_value = '{"photos":{"1":{"token":"max-fixture"}}}'
    assert await sender.media('image', 123) == {'photos': {'1': {'token': 'max-fixture'}}}
    client.upload.assert_not_awaited()


async def test_media_upload_failure_can_retry_before_visible_send(context):
    sender, client, connection = context
    connection.fetchrow.return_value = {'mime_type': 'image/png', 'image_data': b'fixture'}
    client.upload.side_effect = MaxAPIError(0, 'network')
    with pytest.raises(SendError) as caught:
        await sender.media('image', 123)
    assert caught.value.kind == 'retry'
    client.send.assert_not_awaited()


async def test_max_audio_rechecks_card_ownership_at_send(context):
    sender, client, connection = context
    connection.fetchrow.side_effect = [{'ui_language': 'ru'}, None]
    with pytest.raises(SendError) as caught:
        await sender.send(-4000000000000000, {'kind': 'max_audio', 'card_id': 12, 'audio_id': 34})
    assert caught.value.kind == 'rejected'
    client.upload.assert_not_awaited()
    client.send.assert_not_awaited()
