"""Reader regressions without a database: full references and unsuccessful readings."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import dispatch
from app.bot.commands import parse_command
from app.services import passage, reader
from app.services.errors import UserError
from tests.test_bot_ui import message


@pytest.mark.parametrize(('reference', 'note'), [
    ('Песнь песней 1:1', 'заметка'),
    ('Song of Solomon 2:1–2', 'a note with 3 words'),
    ('1 Коринфянам 3:1', 'номер книги'),
    ('Ин3:16', 'без пробела'),
    ('Ин 3:16 - 18', 'важный стих'),
    ('Пс 22', ''),
])
def test_bookmark_split_preserves_complete_reference(reference, note):
    query = reference + (' ' + note if note else '')
    assert reader.split_reference_and_note(query) == (reference, note)
    assert passage.parse_reference(reference) is not None


def test_bookmark_split_bounds_note_and_keeps_invalid_input():
    assert reader.split_reference_and_note('Ин 3:16 ' + 'x' * 400) == ('Ин 3:16', 'x' * reader.MAX_NOTE)
    assert reader.split_reference_and_note('неправильный адрес') == ('неправильный адрес', '')
    with pytest.raises(UserError):
        reader.split_reference_and_note('Ин 3:18–16 заметка')


@pytest.fixture
def command_context(monkeypatch):
    chat = {'telegram_chat_id': 101, 'ui_language': 'ru', 'timezone': 'UTC'}
    monkeypatch.setattr(dispatch, 'destination', AsyncMock(return_value=chat))
    monkeypatch.setattr(dispatch.bible, 'chat_translation', AsyncMock(return_value={'id': 1}))
    mark = AsyncMock()
    monkeypatch.setattr(reader, 'mark_read', mark)
    return mark, SimpleNamespace(max_message_length=3900)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [None, UserError('no_result')])
async def test_invalid_or_missing_read_does_not_mark_day(monkeypatch, command_context, outcome):
    mark, settings = command_context
    monkeypatch.setattr(dispatch.passage, 'lookup', AsyncMock(return_value=outcome if outcome is None else None,
                                                           side_effect=outcome))
    if isinstance(outcome, Exception):
        with pytest.raises(UserError):
            await dispatch.run_command(None, None, settings, message('/read Ин 999'), parse_command('/read Ин 999'))
    else:
        await dispatch.run_command(None, None, settings, message('/read слова'), parse_command('/read слова'))
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_next_enqueue_does_not_mark_day(monkeypatch, command_context):
    mark, settings = command_context
    monkeypatch.setattr(dispatch, 'enqueue_next', AsyncMock(side_effect=UserError('busy')))
    with pytest.raises(UserError):
        await dispatch.run_command(None, None, settings, message('/next'), parse_command('/next'))
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_next_marks_after_enqueue(monkeypatch, command_context):
    mark, settings = command_context
    async def queued(*args):
        mark.assert_not_awaited()
        return 42
    monkeypatch.setattr(dispatch, 'enqueue_next', queued)
    assert await dispatch.run_command(None, None, settings, message('/next'), parse_command('/next')) == (None, None)
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_today_with_no_reading_does_not_mark_day(monkeypatch, command_context):
    mark, settings = command_context
    monkeypatch.setattr(dispatch.readings, 'daily', AsyncMock(return_value=None))
    await dispatch.run_command(None, None, settings, message('/today'), parse_command('/today'))
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_marks_only_after_successful_preparation(monkeypatch, command_context):
    mark, settings = command_context
    result = (passage.Reference('GEN', 1, 1, 1), 'GEN', [{'verse': 1}])
    monkeypatch.setattr(dispatch.passage, 'lookup', AsyncMock(return_value=result))
    monkeypatch.setattr(dispatch.passage, 'render', AsyncMock(return_value='reading'))
    monkeypatch.setattr(dispatch.illustrations, 'decorate', AsyncMock(return_value='reading'))
    monkeypatch.setattr(dispatch.broadcast, 'image_of', AsyncMock(return_value=None))
    async def offer(*args):
        mark.assert_not_awaited()
    monkeypatch.setattr(dispatch.broadcast, 'offer', offer)
    await dispatch.run_command(None, None, settings, message('/read Gen 1:1'), parse_command('/read Gen 1:1'))
    mark.assert_awaited_once()
    mark.reset_mock()
    monkeypatch.setattr(dispatch.illustrations, 'decorate', AsyncMock(side_effect=RuntimeError('db unavailable')))
    with pytest.raises(RuntimeError):
        await dispatch.run_command(None, None, settings, message('/read Gen 1:1'), parse_command('/read Gen 1:1'))
    mark.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['0', '-1', '9999', 'two', '1 extra'])
async def test_bookmark_command_rejects_invalid_page(command_context, value):
    _mark, settings = command_context
    with pytest.raises(UserError):
        await dispatch.run_command(None, None, settings, message('/bookmarks ' + value), parse_command('/bookmarks ' + value))


@pytest.mark.asyncio
async def test_bookmark_command_passes_requested_page(monkeypatch, command_context):
    _mark, settings = command_context
    listing = AsyncMock(return_value='page two')
    monkeypatch.setattr(reader, 'listing', listing)
    assert await dispatch.run_command(None, None, settings, message('/bookmarks 2'), parse_command('/bookmarks 2')) == ('page two', None)
    listing.assert_awaited_once_with(None, 101, {'id': 1}, True, page=2)
