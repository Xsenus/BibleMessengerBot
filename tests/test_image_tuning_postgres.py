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


async def test_request_variant_queues_new_version_with_limit(db):
    c, _, edition, _, row = await setup(db)
    first = await illustrations.store(c, row, edition, jpeg(), 'a')
    await c.execute("INSERT INTO image_generation_jobs(image_id,prompt,prompt_variant,model,state) VALUES($1,'сцена','symbolic','m','ready')", first)
    created = await illustrations.request_variant(c, first)
    assert created and created != first
    assert await c.fetchval('SELECT status FROM verse_illustrations WHERE id=$1', created) == 'pending'
    assert 'Alternative composition' in await c.fetchval('SELECT prompt FROM image_generation_jobs WHERE image_id=$1', created)
    assert await illustrations.request_variant(c, created) is None            # ожидающую версию размножать нельзя
    for _ in range(illustrations.MAX_VARIANTS):
        await c.execute("UPDATE verse_illustrations SET status='ready',image_data=$2,mime_type='image/jpeg' WHERE id=$1 AND status='pending'", created, jpeg())
        created = await illustrations.request_variant(c, first)
        if created is None:
            break
    assert created is None                                                    # достигнут предел версий


async def test_auto_ranking_orders_custom_providers_and_style_is_loaded(db):
    c = db[0]
    await c.execute("INSERT INTO app_settings(key,value) VALUES('images.ranking','auto'),('images.style','oil painting')")
    image_router.RANKING['loaded_at'] = 0.0
    await image_router.load_ranking(c)
    assert image_router.RANKING['mode'] == 'auto' and image_router.RANKING['style'] == 'oil painting'
    image_router.RANKING.update(scores={'x-good': 0.9, 'x-bad': 0.1})
    from types import SimpleNamespace
    good = SimpleNamespace(name='x-good', custom={'priority': 500})
    bad = SimpleNamespace(name='x-bad', custom={'priority': 100})
    assert image_router.rank(None, good) < image_router.rank(None, bad)
    image_router.RANKING.update(mode='priority', style='', loaded_at=0.0)
