"""Закладки, серия чтения и поиск по словам."""
import os
from datetime import date, timedelta

import pytest

from app.services import bible, reader
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
async def test_word_search_falls_back_to_forms_and_any_word(db):
    c, _, edition, _, _ = await setup(db)
    text = await c.fetchval("SELECT text FROM verses WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1", edition['id'])
    words = text.split()
    query = f'{words[0]} qqqqzzzz'
    rows = await bible.search_verses(c, edition['id'], query, 5)
    assert any(r['verse'] == 1 and r['book_code'] == 'GEN' for r in rows)
