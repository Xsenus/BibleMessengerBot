"""Real SQL tests: original message binding, source integrity, safe edit recovery."""

import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import artwork, devotionals, illustrations, readings
from app.services.errors import SendError
from app.worker.delivery import decoded, insert_payload, process_delivery, recover_ambiguous
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import provider, setup
from tests.test_postgres_integration import db as db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]


async def test_initial_outbox_ack_binds_request_before_background_edit(db, monkeypatch):
    provider(monkeypatch)
    c, _, edition, chat, row = await setup(db)
    text = await illustrations.decorate(
        c, "<b>Complete original reading</b>", row, edition, chat=chat, request_key="original"
    )
    assert isinstance(text, illustrations.ReadingText) and text.request_id
    delivery = await insert_payload(
        c, chat, edition, text, "initial", "manual", {"kind": "reading"}
    )
    await illustrations.store(c, row, edition, jpeg(), "ready before initial send")
    assert await artwork.dispatch_requests(c) == 0
    sender = SimpleNamespace(send=AsyncMock(return_value=456))
    assert await process_delivery(c, delivery, sender) == "sent"
    assert await c.fetchval("SELECT telegram_message_id FROM illustration_requests") == 456
    assert await artwork.dispatch_requests(c) == 1
    edit = await c.fetchrow(
        "SELECT * FROM delivery_log WHERE progress->>'kind'='illustration_edit'"
    )
    assert decoded(edit["chunks"])[0]["message_id"] == 456
    assert decoded(edit["chunks"])[0]["text"] == str(text)
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 0


async def test_changed_neighbor_cancels_edit_without_corrupting_original(db, monkeypatch):
    provider(monkeypatch)
    c, _, edition, chat, row = await setup(db)
    neighbor = await c.fetchrow(
        "SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",
        edition["id"],
    )
    reading = readings.attach(row, [row, neighbor])
    await artwork.request_image(c, reading, edition, chat, "Original full text", "source-proof")
    await c.execute("UPDATE illustration_requests SET telegram_message_id=789")
    await illustrations.store(c, row, edition, jpeg(), "same anchor")
    await c.execute(
        "UPDATE verses SET text=text||' changed' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",
        edition["id"],
    )
    assert await artwork.dispatch_requests(c) == 0
    assert await c.fetchval("SELECT state FROM illustration_requests") == "cancelled"
    assert await c.fetchval("SELECT count(*) FROM delivery_log") == 0


async def test_edit_restart_retries_same_target_but_new_sends_stay_uncertain(db):
    c, _, edition, chat, row = await setup(db)
    image = await illustrations.store(c, row, edition, jpeg(), "original")
    edit = await insert_payload(
        c,
        chat,
        edition,
        "full text",
        "edit",
        "illustration_edit",
        {"kind": "illustration_edit"},
        frozen_chunks=[
            {"kind": "rich_edit", "image_id": image, "message_id": 456, "text": "full text"}
        ],
    )
    original = await insert_payload(
        c, chat, edition, "new text", "send", "manual", {"kind": "reading"}
    )
    await c.execute("UPDATE delivery_log SET status='sending',sending_chunk=0")
    assert await recover_ambiguous(c) == 1
    assert await c.fetchval("SELECT status FROM delivery_log WHERE id=$1", original) == "uncertain"
    assert await c.fetchval("SELECT status FROM delivery_log WHERE id=$1", edit) == "retry"
    sender = SimpleNamespace(send=AsyncMock(return_value=456))
    assert await process_delivery(c, edit, sender) == "sent"
    assert sender.send.await_args.args[1]["message_id"] == 456
    assert await c.fetchval("SELECT telegram_message_ids FROM delivery_log WHERE id=$1", edit) == [
        456
    ]


async def test_edit_timeout_is_retryable_and_does_not_advance_reading_progress(db):
    c, _, edition, chat, row = await setup(db)
    image = await illustrations.store(c, row, edition, jpeg(), "original")
    edit = await insert_payload(
        c,
        chat,
        edition,
        "full text",
        "edit",
        "illustration_edit",
        {"kind": "illustration_edit"},
        frozen_chunks=[
            {"kind": "rich_edit", "image_id": image, "message_id": 456, "text": "full text"}
        ],
    )
    sender = SimpleNamespace(send=AsyncMock(side_effect=SendError("retry", 3)))
    assert await process_delivery(c, edit, sender) == "retry"
    assert await c.fetchval("SELECT status FROM delivery_log WHERE id=$1", edit) == "retry"
    assert await c.fetchval("SELECT count(*) FROM chat_reading_progress") == 0
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 0


async def test_devotional_freezes_complete_unit_and_checks_neighbor_changes(db):
    c, _, edition, _, _ = await setup(db)
    day = date(2026, 10, 8)
    morning, chosen = await devotionals.selected_verse(c, edition, 101, day, "morning_verse")
    evening, _ = await devotionals.selected_verse(c, edition, 101, day, "evening_verse")
    assert len(morning["reading_rows"]) == 2
    assert {devotionals.coordinates(r) for r in morning["reading_rows"]}.isdisjoint(
        devotionals.coordinates(r) for r in evening["reading_rows"]
    )
    again, _ = await devotionals.selected_verse(c, edition, 101, day, "morning_verse")
    assert again["reading_rows"] == morning["reading_rows"]
    neighbor = next(r for r in morning["reading_rows"] if r["verse"] != chosen["verse"])
    await c.execute(
        "UPDATE verses SET text=text||$5 WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4",
        edition["id"],
        neighbor["book_code"],
        neighbor["chapter"],
        neighbor["verse"],
        " altered",
    )
    with pytest.raises(ValueError, match="Prepared reading source changed"):
        await devotionals.selected_verse(c, edition, 101, day, "morning_verse")
