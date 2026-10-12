"""Иллюстрации диапазонов стихов: каждая точная последовательность — своя картинка, закреплённая за ней."""
import os
from unittest.mock import AsyncMock

import pytest

from app.services import artwork, illustrations, image_quality, passage
from app.services import image_router as router
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


async def prepare(db):
    c, settings, edition, chat, row = await setup(db)
    await c.execute('''INSERT INTO verses(translation_id,book_code,chapter,verse,text,source_line)
        SELECT $1,'GEN',1,g,'Verse text number '||g,900000+g FROM generate_series(3,12) g''', edition['id'])
    return c, edition, chat


async def rows_for(c, edition, first, last):
    return await passage.fetch_rows(c, edition, 'GEN', 1, first, last)


async def card(c, edition, chat, first, last, key):
    rows = await rows_for(c, edition, first, last)
    text = await passage.render(c, edition, (passage.Reference('GEN', 1, first, last), 'GEN', rows), 'en')
    return await illustrations.decorate_range(c, text, edition, rows, chat=dict(chat), request_key=key)


async def test_every_exact_range_gets_its_own_pinned_picture(db):
    c, edition, chat = await prepare(db)
    cards = [await card(c, edition, chat, 1, last, f'req-{last}') for last in (10, 9, 5)]
    images = await c.fetch("SELECT * FROM verse_illustrations WHERE artwork_scope='range' ORDER BY verse_end DESC")
    assert [(i['verse'], i['verse_end']) for i in images] == [(1, 10), (1, 9), (1, 5)]
    assert len({i['text_sha256'] for i in images}) == 3                      # разные последовательности — разные хеши
    assert all(isinstance(x, illustrations.ReadingText) for x in cards)
    jobs = await c.fetch('SELECT * FROM image_generation_jobs ORDER BY id')
    assert len(jobs) == 3 and all(j['prompt_version'] == 4 and '<passage>' in j['prompt'] for j in jobs)
    assert 'GEN 1:1-9' in jobs[1]['prompt'] and 'number 9' in jobs[1]['prompt'] and 'number 10' not in jobs[1]['prompt']
    await card(c, edition, chat, 1, 9, 'req-again')                          # повтор того же диапазона ничего не создаёт
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE artwork_scope='range'") == 3
    assert await c.fetchval('SELECT count(*) FROM image_generation_jobs') == 3


async def test_range_equal_to_whole_chapter_is_still_a_range_picture(db):
    c, edition, chat = await prepare(db)
    await card(c, edition, chat, 1, 12, 'whole')          # картинку главы запрашивает только адрес целой главы
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE artwork_scope='range'") == 1
    assert await c.fetchval("SELECT count(*) FROM verse_illustrations WHERE artwork_scope='chapter'") == 0


async def test_source_row_roundtrip_and_text_change_detection(db):
    c, edition, chat = await prepare(db)
    await card(c, edition, chat, 2, 6, 'r')
    image = await c.fetchrow("SELECT * FROM verse_illustrations WHERE artwork_scope='range'")
    source = await illustrations.source_row(c, image)
    assert illustrations.identity(source, edition)[4] == image['text_sha256']
    await c.execute("UPDATE verses SET text='changed' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=4", edition['id'])
    changed = await illustrations.source_row(c, image)
    assert illustrations.identity(changed, edition)[4] != image['text_sha256']
    await c.execute("DELETE FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=6", edition['id'])
    assert await illustrations.source_row(c, image) is None                  # пропавший стих — источник недоступен


async def test_generated_range_picture_edits_waiting_card_and_serves_next_user(db, monkeypatch):
    c, edition, chat = await prepare(db)
    first = await card(c, edition, chat, 1, 5, 'first-request')
    assert first.image_id is None and first.request_id                       # текст сразу, картинка позже
    job = await c.fetchval('SELECT id FROM image_generation_jobs')
    monkeypatch.setattr(image_quality, 'gate', AsyncMock(return_value='approved'))
    generator = AsyncMock(return_value=(jpeg(), {}, 'req'))
    settings = artwork.ArtSettings(provider='auto', key='fixture', extra_keys={})
    assert await router.process_job(c, job, settings, generator=generator) == 'ready'
    assert 'GEN 1:1-5' in generator.await_args.args[1] or '<passage>' in generator.await_args.args[1]
    await c.execute("UPDATE illustration_requests SET telegram_message_id=77 WHERE id=$1", first.request_id)
    assert await artwork.dispatch_requests(c) == 1                            # карточка получит картинку правкой
    again = await card(c, edition, chat, 1, 5, 'second-user')
    assert again.image_id == await c.fetchval("SELECT id FROM verse_illustrations WHERE artwork_scope='range' AND status='ready'")


async def test_single_verse_and_chapter_behaviour_unchanged(db):
    c, edition, chat = await prepare(db)
    row = await c.fetchrow("SELECT * FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=3", edition['id'])
    await illustrations.decorate(c, 'Verse', dict(row), edition, chat=dict(chat), request_key='single')
    assert await c.fetchval("SELECT artwork_scope FROM verse_illustrations") == 'verse'
    assert await c.fetchval('SELECT verse_end FROM verse_illustrations') is None
