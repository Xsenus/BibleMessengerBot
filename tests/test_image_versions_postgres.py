"""Real isolated PostgreSQL: shared paid work, calendar ageing, archive and follow-ups."""

import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.bot import handlers
from app.bot.commands import parse_command
from app.bot.transport import TelegramSender
from app.services import artwork, illustrations,readings
from app.services.destinations import configure_chat
from app.worker.delivery import decoded, process_delivery
from tests.test_devotional_artwork import jpeg
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]


async def setup(db):
    c, settings, path = db
    edition, _, _ = await load_fixture(c, path)
    await create_destination(c)
    chat = await configure_chat(c, 101, actor_id=101, translation_id=edition["id"])
    row = await c.fetchrow(
        "SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",
        edition["id"],
    )
    return c, settings, edition, chat, row


def provider(monkeypatch):
    monkeypatch.setenv("ILLUSTRATION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("IMAGE_DAILY_MAX_REQUESTS", "10")


async def test_versions_preserve_binary_file_cache_and_rotate_per_chat(db):
    c, _, edition, _, row = await setup(db)
    first = await illustrations.store(c, row, edition, jpeg(), "first")
    await c.execute(
        "UPDATE verse_illustrations SET telegram_file_id='saved-file',telegram_bot_id=123,s3_backed_up_at=now() WHERE id=$1",
        first,
    )
    old = dict(await c.fetchrow("SELECT * FROM verse_illustrations WHERE id=$1", first))
    second = await illustrations.store(c, row, edition, b"\x89PNG\r\n\x1a\nsecond", "second")
    assert second != first
    assert dict(await c.fetchrow("SELECT * FROM verse_illustrations WHERE id=$1", first)) == old
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == second
    await illustrations.record_view(c, 101, second)
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == first
    await illustrations.record_view(c, 101, first)
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == second
    await create_destination(c, 202)
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=202) == second
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE status='pending'") == 0
    assert await c.fetchval("SELECT count(*) FROM image_generation_jobs") == 0
    with pytest.raises(ValueError, match="preserved"):
        await illustrations.store(c, row, edition, jpeg(), "replace", image_id=first)


@pytest.mark.parametrize(
    "age,fresh",
    [
        ("now()-interval '3 months'+interval '1 second'", True),
        ("now()-interval '3 months'", False),
        ("now()-interval '3 months'-interval '1 day'", False),
    ],
)
async def test_calendar_month_boundary_and_telegram_cache_do_not_reset_age(db, age, fresh):
    c, _, edition, _, row = await setup(db)
    identifier = await illustrations.store(c, row, edition, jpeg(), "old")
    async with c.transaction():
        await c.execute(
            f"UPDATE verse_illustrations SET generated_at={age},updated_at=now() WHERE id=$1",
            identifier,
        )
        assert bool(await illustrations.recent(c, row, edition)) is fresh
        assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == identifier
        target = await artwork.enqueue(c, row, edition)
        assert (target == identifier) is fresh
        assert await c.fetchval("SELECT count(*) FROM image_generation_jobs") == (0 if fresh else 1)
        assert await artwork.enqueue(c, row, edition) == target
        assert (
            await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE status='ready'") == 1
        )


async def test_concurrent_readings_share_one_job_and_edit_each_acknowledged_message(db, monkeypatch):
    provider(monkeypatch)
    c, settings, edition, chat, row = await setup(db)
    second_chat = await create_destination(c, 202)
    other = await asyncpg.connect(settings.database_url)
    try:
        assert await asyncio.gather(
            artwork.request_image(c, row, edition, chat, "first text", "first"),
            artwork.request_image(other, row, edition, second_chat, "second text", "second"),
        ) == [True, True]
    finally:
        await other.close()
    await artwork.request_image(c, row, edition, chat, "duplicate", "another-key")
    assert await c.fetchval("SELECT count(*) FROM illustration_requests") == 3
    assert await c.fetchval("SELECT count(*) FROM image_generation_jobs") == 1
    job = await c.fetchval("SELECT id FROM image_generation_jobs")
    generate = AsyncMock(return_value=(jpeg(), {}, "fixture"))
    assert (
        await artwork.process_job(c, job, artwork.ArtSettings(key="fixture"), generator=generate)
        == "ready"
    )
    generate.assert_awaited_once()
    assert await artwork.dispatch_requests(c) == 0  # No original Telegram ACK yet.
    for request in await c.fetch('SELECT * FROM illustration_requests'):
        await illustrations.bind_message(c,request['telegram_chat_id'],request['id'],300+request['id'])
    assert await artwork.dispatch_requests(c) == 3
    assert await artwork.dispatch_requests(c) == 0
    deliveries = await c.fetch("SELECT * FROM delivery_log ORDER BY id")
    assert len(deliveries) == 3
    assert {decoded(r['chunks'])[0]['text'] for r in deliveries}=={'first text','second text','duplicate'}
    assert all(decoded(r['chunks'])[0]['kind']=='rich_edit' for r in deliveries)
    assert len({decoded(r['chunks'])[0]['message_id'] for r in deliveries})==3
    image_ids = {decoded(r["chunks"])[0]["image_id"] for r in deliveries}
    assert len(image_ids) == 1
    sender = SimpleNamespace(send=AsyncMock(return_value=300))
    for r in deliveries:
        assert await process_delivery(c, r["id"], sender) == "sent"
    assert await c.fetchval("SELECT count(*) FROM chat_reading_progress") == 0
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 1


@pytest.mark.parametrize(
    "change,state", [("revision", "queued"), ("source", "cancelled"), ("expiry", "queued")]
)
async def test_late_images_keep_old_targets_but_reject_changed_source(
    db, monkeypatch, change, state
):
    provider(monkeypatch)
    c, _, edition, chat, row = await setup(db)
    await artwork.request_image(c, row, edition, chat, "caption", "request")
    await c.execute('UPDATE illustration_requests SET telegram_message_id=321')
    await illustrations.store(c, row, edition, jpeg(), "manual")
    if change == "revision":
        await c.execute("UPDATE telegram_chats SET revision=revision+1 WHERE telegram_chat_id=101")
    elif change == "source":
        await c.execute(
            "UPDATE verses SET text='changed' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1",
            edition["id"],
        )
    else:
        await c.execute("UPDATE illustration_requests SET expires_at=now()-interval '1 second'")
    assert await artwork.dispatch_requests(c) == int(state=='queued')
    assert await c.fetchval("SELECT state FROM illustration_requests") == state
    assert await c.fetchval("SELECT count(*) FROM delivery_log") == int(state=='queued')


async def test_random_handler_is_fast_then_cached_and_ack_advances_rotation(db, monkeypatch):
    provider(monkeypatch)
    c, settings, edition, chat, row = await setup(db)
    monkeypatch.setattr(handlers, "destination", AsyncMock(return_value=chat))
    monkeypatch.setattr(readings, "choose", AsyncMock(return_value=row))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        chat=SimpleNamespace(id=101, type="private"),
        message_id=100,
        message_thread_id=None,
    )
    text, _ = await handlers.run_command(c, None, settings, message, parse_command("/random"))
    assert "SYNTHETIC" in text and not isinstance(text, illustrations.IllustratedText)
    assert 'queued' not in text and isinstance(text,illustrations.ReadingText) and text.request_id
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 0
    identifier = await illustrations.store(c, row, edition, jpeg(), "new")
    text, _ = await handlers.run_command(c, None, settings, message, parse_command("/random"))
    assert text.image_id == identifier
    monkeypatch.setattr("app.bot.transport.wait_send_slot", AsyncMock())
    bot = SimpleNamespace(
        id=123,
        send_rich_message=AsyncMock(
            return_value=SimpleNamespace(message_id=999, rich_message=SimpleNamespace(blocks=[SimpleNamespace(type='photo',photo=[SimpleNamespace(file_id="cached")])]))
        ),
    )
    sender = TelegramSender(bot, c, settings)
    assert await sender.send(101, illustrations.chunks(text, identifier)[0]) == 999
    assert (
        await c.fetchval("SELECT send_count FROM illustration_views WHERE image_id=$1", identifier)
        == 1
    )
    assert (
        await c.fetchval("SELECT telegram_file_id FROM verse_illustrations WHERE id=$1", identifier)
        == "cached"
    )
    bot.send_rich_message.side_effect = OSError("network")
    from app.services.errors import SendError

    with pytest.raises(SendError):
        await sender.send(101, illustrations.chunks(text, identifier)[0])
    assert (
        await c.fetchval("SELECT send_count FROM illustration_views WHERE image_id=$1", identifier)
        == 1
    )


async def test_prompt_native_context_and_only_unattempted_jobs_upgrade(db):
    c, _, edition, _, row = await setup(db)
    await c.execute(
        "UPDATE verses SET text='Native neighbor: do not fear.' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",
        edition["id"],
    )
    other, _, _ = await load_fixture(c, db[2], "rus")
    await c.execute("UPDATE verses SET text='OTHER EDITION' WHERE translation_id=$1", other["id"])
    image = await artwork.enqueue(c, row, edition)
    saved = await c.fetchrow("SELECT * FROM image_generation_jobs WHERE image_id=$1", image)
    assert (
        "Native neighbor: do not fear." in saved["prompt"]
        and "OTHER EDITION" not in saved["prompt"]
    )
    assert f"<verse>{row['text']}</verse>" in saved["prompt"]
    await c.execute("UPDATE image_generation_jobs SET prompt='legacy',prompt_version=1")
    await artwork.upgrade_queued_prompts(c)
    assert await c.fetchval("SELECT prompt_version FROM image_generation_jobs") == 2
    await c.execute(
        "UPDATE image_generation_jobs SET prompt='paid frozen',prompt_version=1,attempts=1"
    )
    await artwork.upgrade_queued_prompts(c)
    assert await c.fetchval("SELECT prompt FROM image_generation_jobs") == "paid frozen"


async def test_ten_daily_attempts_share_the_same_monthly_budget(db):
    c, _, edition, _, _ = await setup(db)
    rows = await c.fetch(
        "SELECT * FROM verses WHERE translation_id=$1 ORDER BY book_code,chapter,verse LIMIT 12",
        edition["id"],
    )
    jobs = []
    for row in rows:
        image = await artwork.enqueue(c, row, edition)
        jobs.append(
            await c.fetchval("SELECT id FROM image_generation_jobs WHERE image_id=$1", image)
        )
    config = artwork.ArtSettings(daily_requests=10, monthly_usd=Decimal("10"))
    assert all([await artwork.reserve(c, job, config) for job in jobs[:10]])
    assert await artwork.reserve(c, jobs[10], config) is None
    assert await c.fetchval("SELECT sum(reserved_usd) FROM image_generation_attempts") == Decimal(
        "1.00"
    )
    assert (
        await artwork.reserve(
            c, jobs[10], artwork.ArtSettings(daily_requests=100, monthly_usd=Decimal("1.00"))
        )
        is None
    )
