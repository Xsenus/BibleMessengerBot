"""Закладки, серия чтения и поиск по словам."""
import asyncio
import os
from datetime import date, timedelta

import pytest
import asyncpg

from app.services import bible, reader
from app.services.errors import UserError
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


def test_reference_and_note_split():
    assert reader.split_reference_and_note('Ин 3:16 важный стих') == ('Ин 3:16', 'важный стих')
    assert reader.split_reference_and_note('Ин 3:16-18') == ('Ин 3:16-18', '')
    assert reader.split_reference_and_note('Пс 22') == ('Пс 22', '')


@pytest.mark.asyncio
async def test_streak_counts_consecutive_days(db):
    c, _, _, _, _ = await setup(db)
    today = date(2026, 5, 10)
    for delta in (0, 1, 2, 5, 6):
        assert await reader.mark_read(c, 101, today - timedelta(days=delta))
    assert not await reader.mark_read(c, 101, today)
    assert await reader.streak(c, 101, today) == (3, 3, 5)
    assert (await reader.streak(c, 101, today + timedelta(days=1)))[0] == 3     # сегодня ещё можно отметиться
    assert (await reader.streak(c, 101, today + timedelta(days=2)))[0] == 0
    assert await reader.streak(c, 999, today) == (0, 0, 0)


@pytest.mark.asyncio
async def test_bookmarks_add_replace_list_remove(db):
    c, _, edition, _, _ = await setup(db)
    identifier, title = await reader.add_bookmark(c, 101, edition, 'Gen 1:1 начало')
    again, _ = await reader.add_bookmark(c, 101, edition, 'Gen 1:1 новая заметка')
    assert again == identifier and 'Gen' in title or '1:1' in title
    text = await reader.listing(c, 101, edition, True)
    assert 'новая заметка' in text and str(identifier) in text
    assert not await reader.remove_bookmark(c, 202, identifier)                 # чужая закладка недоступна
    assert await reader.remove_bookmark(c, 101, identifier)
    assert 'нет' in await reader.listing(c, 101, edition, True)


@pytest.mark.asyncio
async def test_all_200_bookmarks_remain_accessible_and_existing_note_can_be_updated_at_limit(db):
    c, settings, edition, _, _ = await setup(db)
    rows = await c.fetch('''SELECT book_code,chapter,verse FROM verses
        WHERE translation_id=$1 ORDER BY source_line LIMIT 201''', edition['id'])
    identifiers = []
    for index, row in enumerate(rows[:200], 1):
        identifier, _ = await reader.add_bookmark(c, 101, edition,
            f"{row['book_code']} {row['chapter']}:{row['verse']} unique_note_{index:03}")
        identifiers.append(identifier)
    rendered = []
    for page in range(1, 21):
        text = await reader.listing(c, 101, edition, page=page)
        assert f'Страница {page}/20' in text
        assert ('/bookmarks ' + str(page + 1) in text) == (page < 20)
        assert ('/bookmarks ' + str(page - 1) in text) == (page > 1)
        rendered.append(text)
    combined = '\n'.join(rendered)
    for index in range(1, 201):
        assert combined.count(f'unique_note_{index:03}') == 1
    with pytest.raises(UserError, match='invalid'):
        await reader.listing(c, 101, edition, page=21)
    first = rows[0]
    again, _ = await reader.add_bookmark(c, 101, edition,
        f"{first['book_code']} {first['chapter']}:{first['verse']} updated at limit")
    assert again == identifiers[0]
    assert 'updated at limit' in await reader.listing(c, 101, edition, page=20)
    row = rows[200]
    with pytest.raises(UserError, match='limit'):
        await reader.add_bookmark(c, 101, edition, f"{row['book_code']} {row['chapter']}:{row['verse']} overflow")
    assert await c.fetchval('SELECT count(*) FROM bookmarks WHERE telegram_chat_id=101') == 200
    # Two clients competing for the last slot cannot exceed the per-chat limit.
    await reader.remove_bookmark(c, 101, identifiers[-1])
    connections = [await asyncpg.connect(settings.database_url) for _ in range(2)]
    try:
        additions = await asyncio.gather(*(reader.add_bookmark(connection, 101, edition,
            f"{row['book_code']} {row['chapter']}:{row['verse']} concurrent")
            for connection, row in zip(connections, rows[199:201], strict=True)), return_exceptions=True)
        assert sum(isinstance(result, tuple) for result in additions) == 1
        errors = [result for result in additions if isinstance(result, Exception)]
        assert len(errors) == 1 and isinstance(errors[0], UserError) and errors[0].key == 'limit'
        assert await c.fetchval('SELECT count(*) FROM bookmarks WHERE telegram_chat_id=101') == 200
    finally:
        await asyncio.gather(*(connection.close() for connection in connections))


@pytest.mark.asyncio
async def test_word_search_falls_back_to_forms_and_any_word(db):
    c, _, edition, _, _ = await setup(db)
    text = await c.fetchval("SELECT text FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1", edition['id'])
    words = text.split()
    query = f'{words[0]} qqqqzzzz'
    rows = await bible.search_verses(c, edition['id'], query, 5)
    assert any(r['verse'] == 1 and r['book_code'] == 'GEN' for r in rows)
