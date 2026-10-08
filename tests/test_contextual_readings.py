"""Meaningful boundaries and idempotent rich-message editing, without API spend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot.transport import TelegramSender
from app.services import bible, illustrations, readings
from app.services.errors import SendError


def verse(number, text, **extra):
    return dict(book_code="JHN", chapter=3, verse=number, verse_end=number, text=text, **extra)


@pytest.mark.parametrize(
    "text",
    [
        "тринадцатый Хуппа, четырнадцатый Иешевеву,",
        "двадцатый Иезекиилю,",
        "Сыновья Аарона: Надав, Авиуд,",
    ],
)
def test_roster_fragments_are_not_recommended(text):
    assert readings.fragment(text)


def test_catalog_is_edition_scoped_and_contains_varied_lengths():
    assert readings.curated({"source_name": "getBible/v2", "source_translation_id": "synodal"})
    assert not readings.curated({"source_name": "getBible/v2", "source_translation_id": "web"})
    assert not readings.curated({"source_name": "Other", "source_translation_id": "synodal"})
    sizes = {last - first + 1 for _, _, first, last in readings.catalog()["units"]}
    assert {1, 2, 3, 5} <= sizes
    assert all(1 <= f <= l and l - f < 6 for _, _, f, l in readings.catalog()["units"])


async def test_context_expands_unfinished_sentence_and_preserves_original_text():
    rows = [
        verse(1, "Jesus spoke to his disciples:"),
        verse(2, "Love one another as I have loved you."),
    ]
    c = SimpleNamespace(fetch=AsyncMock(return_value=rows), fetchval=AsyncMock(return_value="John"))
    edition = {"id": 7, "language_code": "eng", "title": "Fixture", "license_type": "public-domain"}
    result = await readings.contextual(c, edition, rows[0])
    assert result["reading_rows"] == rows
    text = await bible.render_verse(c, result, edition, ui_language="en")
    assert "3:1–2" in text and "<b>[1]</b>" in text and "<b>[2]</b>" in text
    assert all(r["text"] in text for r in rows)
    assert c.fetch.await_args.args[1:] == (7, "JHN", 3)


async def test_independent_wisdom_stays_single_verse():
    rows = [
        verse(1, "A gentle answer turns away wrath."),
        verse(2, "The tongue of the wise brings knowledge."),
    ]
    result = await readings.contextual(
        SimpleNamespace(fetch=AsyncMock(return_value=rows)), {"id": 4}, rows[0]
    )
    assert result["reading_rows"] == rows[:1]


async def test_does_not_cut_argument_at_five_or_bridge_missing_verse():
    rows = [verse(n, "Because this is a dependent unfinished sentence,") for n in range(1, 8)]
    c = SimpleNamespace(fetch=AsyncMock(return_value=rows))
    assert await readings.contextual(c, {"id": 9}, rows[4]) is None
    c.fetch.return_value = [verse(1, "Jesus said:"), verse(3, "Love your neighbor as yourself.")]
    assert await readings.contextual(c, {"id": 9}, c.fetch.return_value[0]) is None


def test_reading_over_photo_caption_limit_is_one_complete_card():
    value = illustrations.ReadingText("<b>[1]</b> " + ("😀 Context. " * 300), 12, 34)
    chunks = illustrations.chunks(value, 12)
    assert len(chunks) == 1 and chunks[0]["text"] == value and chunks[0]["kind"] == "rich"
    assert chunks[0]["request_id"] == 34


def adapter(monkeypatch):
    monkeypatch.setattr("app.bot.transport.wait_send_slot", AsyncMock())
    c = SimpleNamespace(
        fetchrow=AsyncMock(
            return_value={
                "mime_type": "image/jpeg",
                "telegram_file_id": "cached",
                "telegram_bot_id": 77,
                "image_data": b"image",
            }
        ),
        execute=AsyncMock(),
    )
    sent = SimpleNamespace(
        message_id=123,
        rich_message=SimpleNamespace(
            blocks=[SimpleNamespace(type="photo", photo=[SimpleNamespace(file_id="new-cache")])]
        ),
    )
    b = SimpleNamespace(
        id=77,
        send_rich_message=AsyncMock(return_value=sent),
        edit_message_text=AsyncMock(return_value=sent),
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
    )
    s = SimpleNamespace(telegram_global_rate_per_second=20, telegram_chat_rate_per_second=1)
    return TelegramSender(b, c, s), b, c


async def test_ready_art_edits_exact_original_id_with_long_complete_text(monkeypatch):
    sender, b, c = adapter(monkeypatch)
    text = "<b>[1]</b> " + "Original text. " * 300 + "\n<b>[2]</b> Last verse."
    assert (
        await sender.send(
            101, {"kind": "rich_edit", "message_id": 123, "image_id": 9, "text": text}, 72
        )
        == 123
    )
    kwargs = b.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 123 and kwargs["chat_id"] == 101
    assert text.replace("\n", "<br>") in kwargs["rich_message"].html
    assert kwargs["rich_message"].media[0].media.media == "cached"
    b.send_photo.assert_not_awaited()
    b.send_message.assert_not_awaited()
    b.send_rich_message.assert_not_awaited()
    assert any("illustration_views" in call.args[0] for call in c.execute.await_args_list)


@pytest.mark.parametrize(
    "error,kind",
    [
        (
            TelegramBadRequest(
                method=EditMessageText(chat_id=101, message_id=123, text="x"),
                message="message to edit not found",
            ),
            "rejected",
        ),
        (
            TelegramNetworkError(
                method=EditMessageText(chat_id=101, message_id=123, text="x"), message="timeout"
            ),
            "retry",
        ),
    ],
)
async def test_deleted_original_never_becomes_new_send_and_timeouts_are_safe_to_retry(
    monkeypatch, error, kind
):
    sender, b, _ = adapter(monkeypatch)
    b.edit_message_text.side_effect = error
    with pytest.raises(SendError) as caught:
        await sender.send(
            101, {"kind": "rich_edit", "message_id": 123, "image_id": 9, "text": "Original"}
        )
    assert caught.value.kind == kind
    b.send_photo.assert_not_awaited()
    b.send_message.assert_not_awaited()
    b.send_rich_message.assert_not_awaited()
    assert b.edit_message_text.await_count == 1


async def test_lost_edit_ack_is_acknowledged_without_duplicate_or_extra_view(monkeypatch):
    sender, b, c = adapter(monkeypatch)
    b.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=101, message_id=123, text="x"),
        message="Bad Request: message is not modified",
    )
    assert (
        await sender.send(
            101, {"kind": "rich_edit", "message_id": 123, "image_id": 9, "text": "Original"}
        )
        == 123
    )
    c.execute.assert_not_awaited()
    b.send_rich_message.assert_not_awaited()


async def test_editable_card_cannot_attach_reply_keyboard(monkeypatch):
    sender, b, _ = adapter(monkeypatch)
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Read")]])
    with pytest.raises(SendError):
        await sender.send(
            101, {"kind": "rich", "image_id": None, "text": "Original"}, reply_markup=keyboard
        )
    b.send_rich_message.assert_not_awaited()
