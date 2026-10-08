"""Real SQL image persistence and scheduled photo checkpoints in disposable DBs."""

import os
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock

import pytest

from app.services import bible, illustrations, passage
from app.services.accounts import upsert_chat, upsert_user
from app.services.subscriptions import create_or_update_subscription
from app.worker.delivery import decoded, prepare_subscription, process_delivery
from tests.test_postgres_integration import db as db
from tests.test_postgres_integration import load_fixture

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-only; not rendered"


async def test_ready_image_is_source_text_bound_and_storage_limit_rolls_back(db, monkeypatch):
    c, _, tmp = db
    edition, _, _ = await load_fixture(c, tmp)
    row = await bible.random_verse(c, edition)
    identifier = await illustrations.store(c, row, edition, PNG, "fixture prompt")
    assert await illustrations.lookup_or_queue(c, row, edition) == identifier
    changed = dict(row)
    changed["text"] += " modified"
    assert await illustrations.lookup_or_queue(c, changed, edition) is None
    monkeypatch.setattr(illustrations, "MAX_STORED_BYTES", len(PNG))
    with pytest.raises(ValueError, match="storage limit"):
        await illustrations.store(c, changed, edition, PNG, "other prompt")
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE status='ready'") == 1


async def test_actual_sql_passage_keeps_reference_and_bracketed_verse(db):
    c, _, tmp = db
    edition, _, _ = await load_fixture(c, tmp)
    result = await passage.lookup(c, edition, "Genesis 1:1–2")
    text = await passage.render(c, edition, result, "en")
    assert "<b>[1]</b>" in text and "<b>[2]</b>" in text
    assert "SYNTHETIC" in text
    assert len(result[2]) == 2


async def test_daily_photo_is_queued_once_and_progress_commits_only_after_delivery(db):
    c, _, tmp = db
    edition, _, _ = await load_fixture(c, tmp)
    await upsert_user(
        c,
        user_id=101,
        username=None,
        first_name="fixture",
        last_name=None,
        language_code="en",
        default_timezone="UTC",
    )
    await upsert_chat(
        c,
        chat_id=101,
        chat_type="private",
        title=None,
        username=None,
        registered_by=101,
        default_timezone="UTC",
        ui_language="en",
    )
    sub = await create_or_update_subscription(
        c,
        chat_id=101,
        created_by=101,
        translation_id=edition["id"],
        mode="verse_of_day",
        send_time=time(9),
        timezone_name="UTC",
    )
    await c.execute(
        "UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1", sub["id"]
    )
    date = (await c.fetchval("SELECT next_run_at FROM subscriptions WHERE id=$1", sub["id"])).date()
    from app.services import readings
    row = await readings.daily(c, edition, "101", date)
    await illustrations.store(c, row, edition, PNG, "fixture prompt")
    delivery = await prepare_subscription(c, sub["id"])
    frozen = await c.fetchrow("SELECT * FROM delivery_log WHERE id=$1", delivery)
    assert decoded(frozen["chunks"])[0]["kind"] == "rich"
    assert decoded(frozen['chunks'])[0]['image_id'] is not None
    assert await prepare_subscription(c, sub["id"]) is None
    assert await c.fetchval("SELECT last_run_at FROM subscriptions WHERE id=$1", sub["id"]) is None
    sender = type("Sender", (), {"send": AsyncMock(return_value=987)})()
    assert await process_delivery(c, delivery, sender) == "sent"
    after = await c.fetchrow("SELECT * FROM subscriptions WHERE id=$1", sub["id"])
    assert after["last_run_at"] is not None and after["next_run_at"] > datetime.now(UTC)
