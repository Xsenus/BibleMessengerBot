"""Reading stays readable while source and license provenance remain available."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import handlers
from app.bot.commands import parse_command
from app.services import bible
from app.services.formatting import plain_text, split_message
from tests.test_bot_ui import message


def edition(**updates):
    value = {
        'id': 1, 'language_code': 'rus', 'source_name': 'getBible/v2',
        'source_translation_id': 'synodal', 'title': 'Synodal', 'short_title': 'synodal',
        'license_type': 'public-domain', 'license_version': '', 'license_url': '',
        'copyright_notice': 'Public Domain', 'copyright_holder': '', 'translated_by': '',
        'publication_url': 'https://getbible.life/synodal',
        'source_file_url': 'https://api.getbible.net/v2/synodal.json',
        'numbering_system': 'native:Synodal', 'coverage': 'full', 'book_count': 78, 'verse_count': 36219,
        'nonempty_verse_count': 36219,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize('license_type', ['Public Domain', 'public-domain', 'CC0', 'CC0 1.0'])
def test_public_domain_reading_footer_is_only_human_title(license_type):
    data = edition(license_type=license_type)
    original = deepcopy(data)
    assert bible.attribution(data, 'ru') == '<i>Синодальный перевод</i>'
    assert data == original


@pytest.mark.parametrize('locale', ['ru', 'en'])
def test_license_command_keeps_readable_source_license_and_numbering(locale):
    data = edition()
    original = deepcopy(data)
    text = bible.license_details(data, locale)
    assert 'Синодальный перевод' in text
    assert data['publication_url'] in text and data['source_file_url'] in text
    assert 'getBible' in text and 'native:' not in text and 'getBible/v2' not in text
    assert ('Общественное достояние' if locale == 'ru' else 'Public domain') in text
    assert ('Синодальная нумерация' if locale == 'ru' else 'numbering of the Synodal') in text
    assert split_message(text)
    assert data == original


@pytest.mark.parametrize('license_type,url,expected', [
    ('by', 'https://creativecommons.org/licenses/by/4.0/', 'CC BY 4.0'),
    ('CC BY-SA 4.0', 'https://creativecommons.org/licenses/by-sa/4.0/', 'CC BY-SA 4.0'),
])
def test_creative_commons_reading_retains_required_attribution(license_type, url, expected):
    data = edition(source_name='HelloAO', source_translation_id='fixture', title='Fixture Edition',
        short_title='machine-slug', license_type=license_type, license_url=url,
        copyright_notice='© 2020 Fixture Publisher', copyright_holder='Fixture Holder', translated_by='Fixture Translators')
    text = bible.attribution(data, 'ru')
    assert '<i>Fixture Edition</i>' in text and 'machine-slug' not in text
    for required in ('© 2020 Fixture Publisher', 'Fixture Holder', 'Fixture Translators', expected, url, data['publication_url']):
        assert required in text
    assert 'native:' not in text
    assert split_message(text)


@pytest.mark.asyncio
async def test_verse_and_chapter_keep_scripture_exact_and_clean_footer():
    data = edition()
    row = {'book_code':'GEN', 'chapter':1, 'verse':1, 'verse_end':1,
           'text':'SYNTHETIC <text> & exact words', 'is_range_continuation':False}
    connection = SimpleNamespace(fetchval=AsyncMock(return_value='Бытие'), fetch=AsyncMock(return_value=[row]))
    for rendered in (await bible.render_verse(connection, row, data),
                     await bible.render_chapter(connection, data, 'GEN', 1)):
        assert row['text'] in plain_text(rendered)
        assert rendered.endswith('<i>Синодальный перевод</i>')
        assert 'public-domain' not in rendered and 'native:' not in rendered and 'Источник:' not in rendered


@pytest.mark.asyncio
async def test_friendly_names_shared_by_settings_menu_start_and_license(monkeypatch):
    data = edition()
    chat = {'telegram_chat_id':101, 'ui_language':'ru', 'timezone':'UTC'}
    monkeypatch.setattr(handlers.bible, 'chat_translation', AsyncMock(return_value=data))
    monkeypatch.setattr(handlers, 'destination', AsyncMock(return_value=chat))
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[data]))
    settings = await handlers.settings_text(connection, chat)
    assert 'Перевод: Синодальный перевод' in settings and 'Язык Библии: Русский' in settings
    menu, markup = await handlers.edition_menu(connection, chat, 0)
    assert 'Синодальный перевод' in menu and '/translation getbible:synodal' in menu
    assert markup.inline_keyboard[0][0].text == 'Синодальный перевод'
    text, _ = await handlers.run_command(connection, None, None, message('/start'), parse_command('/start'))
    assert 'Синодальный перевод' in text and 'Synodal' not in text
    text, _ = await handlers.run_command(connection, None, None, message('/license'), parse_command('/license'))
    assert 'Лицензия:' in text and 'Нумерация:' in text and data['publication_url'] in text


def test_known_world_english_name_and_unrelated_sources_remain_distinct():
    assert bible.display_title(edition(source_translation_id='web', title='World English Bible', short_title='web')) == 'World English Bible'
    assert bible.display_title(edition(source_name='Other Source', title='Different Synodal Text')) == 'Different Synodal Text'
