"""Passage addressing, image boundaries and opt-in daily delivery contracts."""

from tests.bot_patch import patch_bot
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendPhoto

from app.bot import handlers, transport
from app.services import illustrations, passage
from app.services.errors import SendError, UserError
from app.services.formatting import plain_text
from tests.test_bot_ui import message


@pytest.mark.parametrize(
    "query,book,chapter,first,last",
    [
        ("Иоанна 3:16", "JHN", 3, 16, 16),
        ("Ин.3,16", "JHN", 3, 16, 16),
        ("1 Кор 13:4–8", "1CO", 13, 4, 8),
        ("John 3", "JHN", 3, None, None),
        ("Пс 118:55-57", "PSA", 118, 55, 57),
        ("1 Царств 3:1", "1SA", 3, 1, 1),
        ("3 Цар 3:1", "1KI", 3, 1, 1),
        ("GEN 1:1", "GEN", 1, 1, 1),
        ("1John 1:2", "1JN", 1, 2, 2),
    ],
)
def test_reference_addresses_keep_book_numbering_and_range(query, book, chapter, first, last):
    ref = passage.parse_reference(query)
    assert passage.aliases()[passage.normalized(ref.book)] == book
    assert (ref.chapter, ref.first, ref.last) == (chapter, first, last)


@pytest.mark.parametrize("query", ["любовь", "3:16", "Иоанна 0:1", "Иоанна 3:0", "x" * 201])
def test_invalid_or_word_search_is_not_a_reference(query):
    assert passage.parse_reference(query) is None


def test_reverse_range_rejected():
    with pytest.raises(UserError):
        passage.parse_reference("John 3:17–16")


async def test_range_lookup_respects_selected_edition_and_overlapping_source_range():
    row = {"book_code": "JHN", "chapter": 3, "verse": 16, "verse_end": 18, "text": "source <text>"}
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[row]))
    result = await passage.lookup(connection, {"id": 99}, "John 3:17–18")
    assert result[1] == "JHN" and result[2] == [row]
    assert connection.fetch.await_args.args[1:] == (99, "JHN", 3, 17, 18)


async def test_nonexistent_endpoint_is_not_replaced_by_adjacent_verse():
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[{"verse": 16, "verse_end": 16}]))
    with pytest.raises(UserError):
        await passage.lookup(connection, {"id": 1}, "John 3:16–99")


async def test_bare_address_routes_as_read_without_changing_arbitrary_private_text(monkeypatch):
    routed = AsyncMock()
    monkeypatch.setattr(handlers, "command_handler", routed)
    await handlers.private_text_handler(message("Иоанна 3:16"), None, None, None)
    assert routed.await_args.args[0].text == "/read Иоанна 3:16"


def test_photo_caption_boundary_and_long_unicode_are_lossless():
    short = "<b>Verse</b> " + "x" * 1018
    assert len(plain_text(short)) == 1024
    assert illustrations.chunks(short, 1) == [{"kind": "photo", "image_id": 1, "caption": short}]
    long = "<b>Verse</b> " + "😀" * 600
    chunks = illustrations.chunks(long, 1)
    assert chunks[0]["caption"] == "" and "".join(plain_text(s) for s in chunks[1:]) == plain_text(
        long
    )


async def test_missing_picture_never_holds_up_verse_or_triggers_generation():
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={"id": 1, "status": "pending"}))
    value = await illustrations.decorate(
        connection,
        "source text",
        {"text": "text", "book_code": "GEN", "chapter": 1, "verse": 1},
        {"id": 3},
    )
    assert value == "source text" and not hasattr(value, "image_id")


def sender_fixture(monkeypatch, *, cached=False, cache_failure=False):
    monkeypatch.setattr(transport, "wait_send_slot", AsyncMock())
    image = {
        "mime_type": "image/png",
        "telegram_file_id": "existing" if cached else None,
        "telegram_bot_id": 777,
        "image_data": None if cached else b"fixture PNG",
    }
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=image),
        fetchval=AsyncMock(return_value=b"fixture PNG"),
        execute=AsyncMock(side_effect=OSError("cache down") if cache_failure else None),
    )
    sent = SimpleNamespace(message_id=888, photo=[SimpleNamespace(file_id="file")])
    bot = SimpleNamespace(id=777, send_photo=AsyncMock(return_value=sent), send_message=AsyncMock())
    settings = SimpleNamespace(telegram_global_rate_per_second=20, telegram_chat_rate_per_second=1)
    return transport.TelegramSender(bot, connection, settings), bot, connection, sent


async def test_photo_cache_write_failure_does_not_repeat_successful_send(monkeypatch):
    sender, bot, _, _ = sender_fixture(monkeypatch, cache_failure=True)
    assert (
        await sender.send(101, {"kind": "photo", "image_id": 1, "caption": "<b>source</b>"}) == 888
    )
    assert bot.send_photo.await_count == 1
    bot.send_message.assert_not_awaited()


async def test_expired_telegram_file_id_falls_back_once_to_stored_bytes(monkeypatch):
    sender, bot, _, sent = sender_fixture(monkeypatch, cached=True)
    bot.send_photo.side_effect = [
        TelegramBadRequest(
            method=SendPhoto(chat_id=101, photo="existing"), message="bad file identifier"
        ),
        sent,
    ]
    assert await sender.send(101, {"kind": "photo", "image_id": 1, "caption": "source"}) == 888
    assert bot.send_photo.await_count == 2


async def test_photo_timeout_remains_uncertain_and_is_not_automatically_replayed(monkeypatch):
    sender, bot, _, _ = sender_fixture(monkeypatch)
    bot.send_photo.side_effect = TelegramNetworkError(
        method=SendPhoto(chat_id=101, photo="file"), message="timeout"
    )
    with pytest.raises(SendError) as caught:
        await sender.send(101, {"kind": "photo", "image_id": 1, "caption": "source"})
    assert caught.value.kind == "uncertain" and bot.send_photo.await_count == 1


async def test_daily_command_creates_explicit_schedule_in_destination_timezone(monkeypatch):
    chat = {"telegram_chat_id": 101, "ui_language": "ru", "timezone": "Asia/Novosibirsk"}
    patch_bot(monkeypatch, "destination", AsyncMock(return_value=chat))
    monkeypatch.setattr(handlers.bible, "chat_translation", AsyncMock(return_value={"id": 2}))
    create = AsyncMock(
        return_value={"send_time": __import__("datetime").time(9), "timezone": "Asia/Novosibirsk"}
    )
    patch_bot(monkeypatch, "create_or_update_subscription", create)
    from app.bot.commands import parse_command

    text, _ = await handlers.run_command(
        None,
        None,
        SimpleNamespace(default_send_time="09:00"),
        message("/daily"),
        parse_command("/daily"),
    )
    assert create.await_args.kwargs["mode"] == "verse_of_day"
    assert create.await_args.kwargs["timezone_name"] == "Asia/Novosibirsk"
    assert "09:00" in text and "/daily off" in text
