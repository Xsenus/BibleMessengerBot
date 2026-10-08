"""Real SQL tests for distinct choices, race-safe budgets and durable generation."""

import asyncio
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import artwork, bible, devotionals
from app.services.subscriptions import create_or_update_subscription
from app.worker.delivery import decoded, prepare_subscription, process_delivery
from tests.test_devotional_artwork import jpeg
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]


async def setup(c, path):
    edition, _, _ = await load_fixture(c, path)
    chat = await create_destination(c)
    for mode, clock in [
        ("verse_of_day", time(9)),
        ("morning_verse", time(9, 38)),
        ("evening_verse", time(21, 13)),
    ]:
        await create_or_update_subscription(
            c,
            chat_id=101,
            created_by=101,
            translation_id=edition["id"],
            mode=mode,
            send_time=clock,
            timezone_name="Asia/Novosibirsk",
        )
    return edition, chat


async def test_selection_call_order_persistence_and_repeat_exclusion(db):
    c, _, p = db
    edition, _ = await setup(c, p)
    day = date(2026, 10, 8)
    evening = await devotionals.selection(c, edition, 101, day, "evening_verse")
    morning = await devotionals.selection(c, edition, 101, day, "morning_verse")
    daily = await bible.verse_of_day(c, edition, "101", day)
    assert len({devotionals.coordinates(r) for r in [evening, morning, daily]}) == 3
    assert (await devotionals.selection(c, edition, 101, day, "evening_verse"))["id"] == evening[
        "id"
    ]
    tomorrow = await devotionals.selection(
        c, edition, 101, day + timedelta(days=1), "evening_verse"
    )
    assert devotionals.coordinates(tomorrow) not in {
        devotionals.coordinates(evening),
        devotionals.coordinates(morning),
    }


async def test_prefetch_job_image_persistence_and_same_photo_delivery(db):
    c, _, p = db
    edition, _ = await setup(c, p)
    now = datetime.now(UTC)
    await artwork.plan_ahead(c, now=now)
    count = await c.fetchval("SELECT count(*) FROM image_generation_jobs")
    await artwork.plan_ahead(c, now=now)
    assert await c.fetchval("SELECT count(*) FROM image_generation_jobs") == count and count >= 4
    sub = await c.fetchrow("SELECT * FROM subscriptions WHERE mode='morning_verse'")
    await c.execute(
        "UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1", sub["id"]
    )
    day = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Novosibirsk")).date()
    row, _chosen = await devotionals.selected_verse(c, edition, 101, day, "morning_verse")
    job = await c.fetchval(
        """SELECT j.id FROM image_generation_jobs j JOIN verse_illustrations i ON i.id=j.image_id
        WHERE i.translation_id=$1 AND i.book_code=$2 AND i.chapter=$3 AND i.verse=$4""",
        edition["id"],
        row["book_code"],
        row["chapter"],
        row["verse"],
    )
    generator = AsyncMock(return_value=(jpeg(), {"output_tokens": 100}, "fixture-id"))
    assert (
        await artwork.process_job(c, job, artwork.ArtSettings(key="fixture"), generator=generator)
        == "ready"
    )
    assert (
        await artwork.process_job(c, job, artwork.ArtSettings(key="fixture"), generator=generator)
        == "cached"
    )
    generator.assert_awaited_once()
    delivery = await prepare_subscription(c, sub["id"])
    queued = await c.fetchrow("SELECT * FROM delivery_log WHERE id=$1", delivery)
    assert decoded(queued["chunks"])[0]["kind"] == "photo"
    assert escape(row["text"]) in queued["payload_preview"]
    assert (
        await process_delivery(
            c, delivery, type("Sender", (), {"send": AsyncMock(return_value=321)})()
        )
        == "sent"
    )
    after = await c.fetchrow("SELECT * FROM subscriptions WHERE id=$1", sub["id"])
    assert after["next_run_at"].astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Novosibirsk")
    ).time() == time(9, 38)


async def test_budget_reservation_and_uncertainty_block_paid_duplicate(db):
    c, _settings, p = db
    _edition, _ = await setup(c, p)
    await artwork.plan_ahead(c)
    jobs = await c.fetch("SELECT id FROM image_generation_jobs ORDER BY id LIMIT 2")
    config = artwork.ArtSettings(key="fixture", monthly_usd=Decimal("0.10"))

    async def timeout(*args):
        raise artwork.GenerationError("transport_uncertain", uncertain=True)

    assert (
        await artwork.process_job(c, jobs[0]["id"], config, generator=timeout)
        == "transport_uncertain"
    )
    assert (
        await artwork.process_job(c, jobs[1]["id"], config, generator=timeout)
        == "budget_or_not_due"
    )
    assert (
        await artwork.process_job(c, jobs[0]["id"], config, generator=timeout)
        == "budget_or_not_due"
    )
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 1
    assert (
        await c.fetchval("SELECT state FROM image_generation_jobs WHERE id=$1", jobs[0]["id"])
        == "uncertain"
    )


async def test_changed_bible_source_never_gets_queued_old_prompt(db):
    c, _, p = db
    _edition, _ = await setup(c, p)
    await artwork.plan_ahead(c)
    job = await c.fetchrow(
        """SELECT j.id AS job_id,i.* FROM image_generation_jobs j JOIN verse_illustrations i ON i.id=j.image_id LIMIT 1"""
    )
    await c.execute(
        "UPDATE verses SET text=text||$5 WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4",
        job["translation_id"],
        job["book_code"],
        job["chapter"],
        job["verse"],
        " changed",
    )
    generator = AsyncMock()
    assert (
        await artwork.process_job(
            c, job["job_id"], artwork.ArtSettings(key="fixture"), generator=generator
        )
        == "source_changed"
    )
    generator.assert_not_awaited()


async def test_two_connections_cannot_overspend_last_reservation(db):
    c, settings, p = db
    await setup(c, p)
    await artwork.plan_ahead(c)
    jobs = await c.fetch("SELECT id FROM image_generation_jobs ORDER BY id LIMIT 2")
    other = await asyncpg.connect(settings.database_url)
    try:
        config = artwork.ArtSettings(monthly_usd=Decimal("0.10"))
        results = await asyncio.gather(
            artwork.reserve(c, jobs[0]["id"], config), artwork.reserve(other, jobs[1]["id"], config)
        )
        assert sum(r is not None for r in results) == 1
        assert await c.fetchval(
            "SELECT sum(reserved_usd) FROM image_generation_attempts"
        ) == Decimal("0.10")
    finally:
        await other.close()


async def test_time_preserves_distinct_devotional_clocks(db, monkeypatch):
    from types import SimpleNamespace

    from app.bot import handlers

    c, settings, path = db
    await setup(c, path)
    chat = await c.fetchrow("SELECT * FROM telegram_chats WHERE telegram_chat_id=101")
    monkeypatch.setattr(handlers, "destination", AsyncMock(return_value=chat))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=101), chat=SimpleNamespace(type="private")
    )
    parsed = SimpleNamespace(name="time", arguments=["10:15", "Asia/Novosibirsk"], target=None)
    await handlers.run_command(c, None, settings, message, parsed)
    clocks = {
        r["mode"]: r["send_time"] for r in await c.fetch("SELECT mode,send_time FROM subscriptions")
    }
    assert clocks == {
        "verse_of_day": time(10, 15),
        "morning_verse": time(9, 38),
        "evening_verse": time(21, 13),
    }
