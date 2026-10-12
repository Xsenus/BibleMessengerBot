"""Молитвослов: меню, подборки отрывков в издании чата, нумерация псалмов."""
import os

import pytest

from app.services import prayerbook
from app.services.errors import UserError
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


def test_menu_lists_every_prayer_in_both_languages():
    for locale, title in (('ru', 'Молитвослов'), ('en', 'Prayer book')):
        text, markup = prayerbook.menu({'ui_language': locale, 'telegram_chat_id': 101})
        buttons = [b for row in markup.inline_keyboard for b in row]
        assert title in text and len(buttons) == len(prayerbook.PRAYERS)
        assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_every_prayer_has_titles_and_scripture_references():
    codes = [p.code for p in prayerbook.PRAYERS]
    assert len(codes) == len(set(codes)) == 7
    for prayer in prayerbook.PRAYERS:
        assert prayer.title['ru'] and prayer.title['en'] and prayer.refs


async def add_verse(c, edition, book, chapter, verse, text):
    template = dict(await c.fetchrow('SELECT * FROM verses WHERE translation_id=$1 AND book_code=$2 LIMIT 1', edition['id'], book))
    template.pop('id', None)
    template.pop('search_text', None)
    template['ordinal'] = await c.fetchval('SELECT max(ordinal)+1 FROM verses WHERE translation_id=$1', edition['id'])
    template.update(chapter=chapter, verse=verse, verse_end=None, text=text,
                    source_line=await c.fetchval('SELECT max(source_line)+1 FROM verses'))
    columns = ','.join(template)
    placeholders = ','.join(f'${i + 1}' for i in range(len(template)))
    await c.execute(f'INSERT INTO verses({columns}) VALUES({placeholders})', *template.values())  # noqa: S608


@pytest.mark.asyncio
async def test_prayer_text_comes_from_chat_edition_with_psalm_renumbering(db):
    c, _, edition, chat, _ = await setup(db)
    chat = await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')
    for verse in range(1, 7):
        await add_verse(c, edition, 'PSA', 22, verse, f'Shepherd psalm verse {verse}')       # короткий 22-й псалом — синодальная нумерация
    await add_verse(c, edition, 'MAT', 6, 9, 'Our Father')
    await add_verse(c, edition, 'MAT', 6, 13, 'Amen verse')
    assert await prayerbook.uses_synodal_psalms(c, edition) is True
    text, markup = await prayerbook.show(c, chat, edition, 'prosperity')
    assert 'Shepherd psalm verse 1' in str(text)
    assert 'Prayer book' in markup.inline_keyboard[0][0].text                                  # язык чата — английский
    text, _ = await prayerbook.show(c, chat, edition, 'ourfather')
    assert 'Our Father' in str(text) and 'Amen verse' in str(text)
    for index in range(7, 32):                                                                 # теперь 22-й псалом длинный — еврейская нумерация
        await add_verse(c, edition, 'PSA', 22, index, f'long {index}')
    assert await prayerbook.uses_synodal_psalms(c, edition) is False
    with pytest.raises(UserError):
        await prayerbook.show(c, chat, edition, 'nope')
    with pytest.raises(UserError):
        await prayerbook.show(c, chat, edition, 'protection')                                  # такого отрывка в издании нет
