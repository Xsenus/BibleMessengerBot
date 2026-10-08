import asyncio
import os
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import artwork
from app.services import image_providers as api
from app.services import image_router as router
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Real PostgreSQL required"),
]


def config(**kwargs):
    return artwork.ArtSettings(
        provider="auto",
        key="fixture-openai",
        extra_keys={name: "fixture-" + name for name in api.NAMES if name != "openai"},
        **kwargs,
    )


async def job(db):
    c, settings, edition, _chat, row = await setup(db)
    await artwork.enqueue(c, row, edition)
    return c, settings, await c.fetchval("SELECT id FROM image_generation_jobs")


@pytest.mark.parametrize(
    "error",
    [
        api.GenerationError("auth"),
        api.GenerationError("quota"),
        api.GenerationError("transport_uncertain", uncertain=True),
        api.GenerationError("server_uncertain", uncertain=True),
        api.GenerationError("rate_limited", retry=True, retry_after=900),
    ],
)
async def test_failover_records_every_reserve_and_updates_shared_image(db, error):
    c, _, identifier = await job(db)
    settings = config()
    await router.configure(c, settings)
    seen = []

    async def generator(provider, prompt, **kwargs):
        seen.append(provider.name)
        if len(seen) == 1:
            raise error
        return jpeg(), {}, "second-provider-id"

    assert await router.process_job(c, identifier, settings, generator=generator) == error.code
    assert await router.process_job(c, identifier, settings, generator=generator) == "ready"
    assert seen == ["openai", "gemini"]
    attempts = await c.fetch("SELECT * FROM image_generation_attempts ORDER BY id")
    assert attempts[0]["state"] == ("uncertain" if error.uncertain else "failed")
    assert attempts[1]["state"] == "succeeded"
    assert sum(r["reserved_usd"] for r in attempts) == Decimal("0.50")
    assert await c.fetchval("SELECT status FROM verse_illustrations") == "ready"
    assert (
        await router.process_job(c, identifier, settings, generator=generator)
        == "budget_or_not_due"
    )
    assert len(seen) == 2


async def test_all_five_are_tried_once_and_missing_keys_are_skipped(db):
    c, _, identifier = await job(db)
    settings = config()
    seen = []

    async def reject(provider, *args, **kwargs):
        seen.append(provider.name)
        raise api.GenerationError("quota")

    for _ in range(7):
        await router.process_job(c, identifier, settings, generator=reject)
    assert seen == list(api.NAMES)
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 5
    assert await c.fetchval("SELECT sum(reserved_usd) FROM image_generation_attempts") == Decimal(
        "0.80"
    )


async def test_budget_stops_fallback_and_concurrent_reservations_share_limit(db):
    c, dbsettings, identifier = await job(db)
    settings = config(monthly_usd=Decimal("0.10"))
    other = await asyncpg.connect(dbsettings.database_url)
    try:
        results = await asyncio.gather(
            router.reserve(c, identifier, settings), router.reserve(other, identifier, settings)
        )
        assert sum(r[0] is not None for r in results) == 1
        attempt, provider, _ = next(r for r in results if r[0])
        await router.fail(c, identifier, attempt, provider, api.GenerationError("quota"))
        generator = AsyncMock()
        assert (
            await router.process_job(c, identifier, settings, generator=generator)
            == "budget_or_not_due"
        )
        generator.assert_not_awaited()
        assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 1
    finally:
        await other.close()


async def test_daily_limit_is_shared_across_fallback_providers(db):
    c, _, identifier = await job(db)
    settings = config(daily_requests=1)
    failed = AsyncMock(side_effect=api.GenerationError("quota"))
    assert await router.process_job(c, identifier, settings, generator=failed) == "quota"
    assert (
        await router.process_job(c, identifier, settings, generator=failed) == "budget_or_not_due"
    )
    assert failed.await_count == 1


async def test_moderation_does_not_route_around_rejection(db):
    c, _, identifier = await job(db)
    failed = AsyncMock(side_effect=api.GenerationError("moderation"))
    assert await router.process_job(c, identifier, config(), generator=failed) == "moderation"
    assert (
        await router.process_job(c, identifier, config(), generator=failed) == "budget_or_not_due"
    )
    assert failed.await_count == 1


async def test_restart_resumes_async_task_without_paid_reservation(db):
    c, _, identifier = await job(db)
    settings = replace(config(), provider_order=("bfl",))
    calls = []

    async def pending(provider, prompt, *, accepted, resume):
        calls.append(resume)
        if not resume:
            await accepted("remote-id", "https://api.eu.bfl.ai/v1/get_result?id=remote-id")
            raise api.GenerationError("remote_pending", retry=True)
        return jpeg(), {}, "remote-id"

    assert await router.process_job(c, identifier, settings, generator=pending) == "remote_pending"
    await c.execute("UPDATE image_generation_jobs SET state='running',retry_at=NULL")
    await router.recover(c)
    assert await router.process_job(c, identifier, settings, generator=pending) == "ready"
    assert calls[1]["id"] == "remote-id"
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 1


async def test_legacy_uncertainty_and_persistence_failure_are_not_automatically_retried(db):
    c, _, identifier = await job(db)
    await c.execute(
        "UPDATE image_generation_jobs SET state='uncertain',error_code='transport_uncertain'"
    )
    await router.configure(c, config())
    await router.recover(c)
    generator = AsyncMock()
    assert (
        await router.process_job(c, identifier, config(), generator=generator)
        == "budget_or_not_due"
    )
    generator.assert_not_awaited()
    await c.execute("UPDATE image_generation_jobs SET state='queued'")
    broken = AsyncMock(side_effect=RuntimeError("fixture persistence"))
    with pytest.raises(RuntimeError):
        await router.process_job(c, identifier, config(), generator=broken)
    await router.configure(c, config())
    assert await c.fetchval("SELECT state FROM image_generation_jobs") == "uncertain"


async def test_rotating_key_resets_only_its_breaker(db):
    c, _, _identifier = await job(db)
    settings = config()
    await router.configure(c, settings)
    await c.execute(
        "UPDATE image_provider_health SET blocked_until=now()+interval '1 day',error_code='auth'"
    )
    await router.configure(
        c, replace(settings, extra_keys={**settings.extra_keys, "gemini": "new-fixture-key"})
    )
    assert (
        await c.fetchval("SELECT blocked_until FROM image_provider_health WHERE provider='gemini'")
        is None
    )
    assert await c.fetchval(
        "SELECT blocked_until IS NOT NULL FROM image_provider_health WHERE provider='openai'"
    )


async def test_source_change_during_external_generation_prevents_publication(db):
    c, _, identifier = await job(db)

    async def changed(*args, **kwargs):
        await c.execute("UPDATE verses SET text=text||' changed'")
        return jpeg(), {}, "paid-but-stale"

    assert await router.process_job(c, identifier, config(), generator=changed) == "source_changed"
    assert await c.fetchval("SELECT status FROM verse_illustrations") == "pending"
    assert await c.fetchval("SELECT state FROM image_generation_attempts") == "succeeded"


async def test_no_keys_does_not_reserve_and_cooldown_chooses_another_service(db):
    c, _, identifier = await job(db)
    empty = artwork.ArtSettings(provider="auto")
    generator = AsyncMock(return_value=(jpeg(), {}, "fixture-id"))
    assert (
        await router.process_job(c, identifier, empty, generator=generator)
        == "providers_unavailable"
    )
    generator.assert_not_awaited()
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 0
    await c.execute("UPDATE image_generation_jobs SET retry_at=NULL")
    await router.configure(c, config())
    await c.execute(
        "UPDATE image_provider_health SET blocked_until=now()+interval '1 day' WHERE provider='openai'"
    )
    assert await router.process_job(c, identifier, config(), generator=generator) == "ready"
    assert generator.await_args.args[0].name == "gemini"


async def test_async_deadline_fails_over_with_both_paid_reserves_retained(db):
    c, _, identifier = await job(db)
    settings = replace(config(), provider_order=("bfl", "stability"))

    async def pending(provider, prompt, *, accepted, resume):
        if provider.name == "bfl":
            await accepted("remote-id", "https://api.bfl.ai/v1/get_result?id=remote-id")
            raise api.GenerationError("remote_pending", retry=True)
        return jpeg(), {}, "stability-id"

    assert await router.process_job(c, identifier, settings, generator=pending) == "remote_pending"
    await c.execute("UPDATE image_generation_attempts SET created_at=now()-interval '16 minutes'")
    await c.execute("UPDATE image_generation_jobs SET retry_at=NULL")
    assert await router.process_job(c, identifier, settings, generator=pending) == "remote_timeout"
    assert await router.process_job(c, identifier, settings, generator=pending) == "ready"
    assert await c.fetchval("SELECT count(*) FROM image_generation_attempts") == 2
    assert (
        await c.fetchval("SELECT state FROM image_generation_attempts WHERE provider='bfl'")
        == "uncertain"
    )


async def test_different_jobs_compete_for_the_same_last_budget_reservation(db):
    c, dbsettings, first = await job(db)
    from app.services import bible

    image = await c.fetchrow("SELECT * FROM verse_illustrations")
    edition = await bible.find_translation(c, image["translation_id"])
    row = await c.fetchrow(
        "SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2",
        edition["id"],
    )
    await artwork.enqueue(c, row, edition)
    second = await c.fetchval("SELECT id FROM image_generation_jobs WHERE id<>$1", first)
    other = await asyncpg.connect(dbsettings.database_url)
    try:
        settings = config(monthly_usd=Decimal("0.10"))
        results = await asyncio.gather(
            router.reserve(c, first, settings), router.reserve(other, second, settings)
        )
        assert sum(r[0] is not None for r in results) == 1
        assert await c.fetchval(
            "SELECT sum(reserved_usd) FROM image_generation_attempts"
        ) == Decimal("0.10")
    finally:
        await other.close()


async def test_alternative_provider_result_updates_all_original_waiting_cards(db):
    from tests.test_persistent_artwork_postgres import request
    from tests.test_postgres_integration import create_destination

    c, _, edition, chat, row = await setup(db)
    second = await create_destination(c, 202)
    await request(c, row, edition, chat, "first", 401)
    await request(c, row, edition, second, "second", 402)
    identifier = await c.fetchval("SELECT id FROM image_generation_jobs")
    generator = AsyncMock(side_effect=[api.GenerationError("quota"), (jpeg(), {}, "gemini-result")])
    assert await router.process_job(c, identifier, config(), generator=generator) == "quota"
    assert await router.process_job(c, identifier, config(), generator=generator) == "ready"
    assert await artwork.dispatch_requests(c) == 2
    from app.worker.delivery import decoded

    deliveries = await c.fetch("SELECT chunks FROM delivery_log WHERE mode='illustration_edit'")
    assert sorted(decoded(d["chunks"])[0]["message_id"] for d in deliveries) == [401, 402]
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE status='ready'") == 1
