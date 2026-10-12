"""Статистика генераторов, закрепление и карантин версий картинок."""
import os

import pytest

from app.services import illustrations, image_router
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


async def test_provider_stats_score_prefers_reliable_fast_provider(db):
    c, _, edition, _, row = await setup(db)
    image = await illustrations.pending(c, row, edition)
    job = await c.fetchval("INSERT INTO image_generation_jobs(image_id,prompt,prompt_variant,model) VALUES($1,'p','v','m') RETURNING id", image['id'])
    for provider, states in (('good', ['succeeded'] * 9 + ['failed']), ('bad', ['failed'] * 8 + ['succeeded'] * 2)):
        for state in states:
            await c.execute("INSERT INTO image_generation_attempts(job_id,reserved_usd,state,provider,duration_ms) VALUES($1,0.01,$2,$3,5000)", job, state, provider)
    await c.execute("INSERT INTO image_generation_attempts(job_id,reserved_usd,state,provider,error_code) VALUES($1,0.01,'succeeded','bad','quality_text')", job)
    stats = await image_router.provider_stats(c)
    assert stats['good']['score'] > stats['bad']['score']
    assert stats['bad']['rejected'] == 1 and stats['good']['success_rate'] == 0.9


async def test_pin_and_quarantine_control_which_version_is_shown(db):
    c, _, edition, _, row = await setup(db)
    first = await illustrations.store(c, row, edition, jpeg(), 'a')
    second = await illustrations.store(c, row, edition, b'\x89PNG\r\n\x1a\nsecond', 'b')
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == second
    assert await illustrations.set_pinned(c, first, True)
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == first
    assert await illustrations.set_pinned(c, second, True)               # закрепление переходит к новой версии
    assert not await c.fetchval('SELECT pinned FROM verse_illustrations WHERE id=$1', first)
    assert await illustrations.set_quarantine(c, second, True)
    assert not await c.fetchval('SELECT pinned FROM verse_illustrations WHERE id=$1', second)
    assert await illustrations.lookup_or_queue(c, row, edition, chat_id=101) == first
    assert not await illustrations.set_pinned(c, 999999, True)
