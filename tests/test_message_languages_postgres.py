"""Real disposable PostgreSQL, fake Telegram: persistent per-message language changes."""
import os
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import handlers
from app.bot.commands import parse_command
from app.bot.transport import TelegramSender
from app.services import artwork, bible, illustrations, message_languages as languages
from app.services.errors import UserError
from app.services.subscriptions import create_or_update_subscription
from app.worker.delivery import decoded, prepare_subscription, process_delivery, recover_ambiguous
from tests.test_bot_ui import message, pool_for
from tests.test_devotional_artwork import jpeg
from tests.test_image_versions_postgres import setup
from tests.test_postgres_integration import db as db, load_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='Real PostgreSQL required')]


def sender(c, settings, monkeypatch):
    async def sent(**kwargs):
        return SimpleNamespace(message_id=kwargs.get('message_id', 501), rich_message=SimpleNamespace(blocks=[]))
    bot = SimpleNamespace(id=123, send_rich_message=AsyncMock(side_effect=sent), edit_message_text=AsyncMock(side_effect=sent))
    monkeypatch.setattr('app.bot.transport.wait_send_slot', AsyncMock())
    return bot, TelegramSender(bot, c, settings)


async def card(c, edition, chat, row, *, image=None, request=None):
    text = await bible.render_verse(c, row, edition, ui_language='en')
    text = languages.inherit(illustrations.ReadingText(text, image, request), text)
    chunk = (await languages.prepare(c, text, illustrations.chunks(text, image, 3900), chat))[0]
    await languages.bind(c, chunk['card_id'], chat['telegram_chat_id'], 501)
    return chunk, await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', chunk['card_id'])


async def test_same_message_same_image_persistent_choice_and_stale_edits(db, monkeypatch):
    c, settings, en, chat, row = await setup(db)
    ru, _, _ = await load_fixture(c, db[2], 'rus')
    await c.execute("UPDATE verses SET text='Русский текст <не HTML>.' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1", ru['id'])
    image = await illustrations.store(c, row, en, jpeg(), 'fixture')
    _, original = await card(c, en, chat, row, image=image)
    before = dict(await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101'))
    bot, transport = sender(c, settings, monkeypatch)
    first = await languages.select(c, 101, 501, original['id'], 's', ru['id'])
    updated = await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', original['id'])
    assert 'Русский текст &lt;не HTML&gt;' in updated['current_html']
    assert updated['selected_translation_id'] == ru['id'] and updated['image_id'] == image
    # Simulate another choice before an old pending delivery runs.
    second = await languages.select(c, 101, 501, original['id'], 's', en['id'])
    assert await process_delivery(c, second, transport) == 'sent'
    assert await process_delivery(c, first, transport) == 'sent'
    for call in bot.edit_message_text.await_args_list:
        assert call.kwargs['message_id'] == 501 and call.kwargs['chat_id'] == 101
        assert original['original_html'].replace('\n', '<br>') in call.kwargs['rich_message'].html
        assert len(call.kwargs['rich_message'].media) == 1
        assert call.kwargs['reply_markup'].inline_keyboard
    bot.send_rich_message.assert_not_awaited()
    assert dict(await c.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=101')) == before
    assert await c.fetchval('SELECT count(*) FROM chat_reading_progress') == 0
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts') == 0
    assert await c.fetchval('SELECT current_html FROM reading_cards WHERE id=$1', original['id']) == original['original_html']


async def test_late_image_keeps_new_language_and_buttons(db, monkeypatch):
    monkeypatch.setenv('ILLUSTRATION_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    c, settings, en, chat, row = await setup(db)
    ru, _, _ = await load_fixture(c, db[2], 'rus')
    await c.execute("UPDATE verses SET text='Переведённый стих.' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1", ru['id'])
    text = await illustrations.decorate(c, await bible.render_verse(c, row, en), row, en, chat=chat, request_key='pending')
    chunk = (await languages.prepare(c, text, illustrations.chunks(text, None, 3900), chat))[0]
    await languages.bind(c, chunk['card_id'], 101, 501)
    await illustrations.bind_message(c, 101, text.request_id, 501)
    change = await languages.select(c, 101, 501, chunk['card_id'], 's', ru['id'])
    bot, transport = sender(c, settings, monkeypatch)
    assert await process_delivery(c, change, transport) == 'sent'
    image = await illustrations.store(c, row, en, jpeg(), 'fixture')
    assert await artwork.dispatch_requests(c) == 1
    edit = await c.fetchval('SELECT delivery_id FROM illustration_requests WHERE id=$1', text.request_id)
    assert await process_delivery(c, edit, transport) == 'sent'
    final = bot.edit_message_text.await_args.kwargs
    assert 'Переведённый стих.' in final['rich_message'].html
    assert len(final['rich_message'].media) == 1
    assert any('✓' in b.text for r in final['reply_markup'].inline_keyboard for b in r)
    assert await c.fetchval('SELECT image_id FROM reading_cards WHERE id=$1', chunk['card_id']) == image
    assert await c.fetchval('SELECT count(*) FROM image_generation_attempts') == 0
    bot.send_rich_message.assert_not_awaited()


async def test_pager_and_exact_coverage_security(db):
    c, _, en, chat, row = await setup(db)
    for code in ['rus', 'deu', 'fra', 'spa', 'ita', 'por', 'hbo']:
        await load_fixture(c, db[2], code)
    chunk, original = await card(c, en, chat, row)
    editions = await languages.available(c, original)
    assert len(editions) == 8
    markup = await languages.keyboard(c, original)
    assert len([b for r in markup.inline_keyboard for b in r if ':s:' in b.callback_data]) == 6
    assert all(len(b.callback_data.encode()) <= 64 for r in markup.inline_keyboard for b in r)
    await languages.select(c, 101, 501, original['id'], 'p', 1)
    state = await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', original['id'])
    markup = await languages.keyboard(c, state)
    assert len([b for r in markup.inline_keyboard for b in r if ':s:' in b.callback_data]) == 2
    assert state['current_html'] == original['current_html']
    for args in [(202,501,original['id'],'s',en['id']), (101,999,original['id'],'p',0), (101,501,original['id'],'p',5), (101,501,original['id'],'s',999999), (101,501,original['id'],'x',0)]:
        with pytest.raises(UserError):
            await languages.select(c, *args)
    # A translation with a missing verse inside a requested interval is excluded.
    ru = next(e for e in editions if e['language_code'] == 'rus')
    await c.execute("UPDATE reading_cards SET refs='[{\"book\":\"GEN\",\"chapter\":1,\"first\":1,\"last\":2}]'::jsonb WHERE id=$1", chunk['card_id'])
    await c.execute("UPDATE verses SET text='' WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=2", ru['id'])
    state = await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', original['id'])
    assert ru['id'] not in [e['id'] for e in await languages.available(c, state)]


@pytest.mark.parametrize('command', ['/read Genesis 1', '/read Genesis 1:1', '/search Genesis 1:1-2', '/search SYNTHETIC'])
async def test_reference_and_word_search_every_fragment_has_references(db, command):
    c, settings, en, chat, _ = await setup(db)
    result, _ = await handlers.run_command(c, None, settings, message(command), parse_command(command))
    assert hasattr(result, 'refs')
    chunks = await languages.prepare(c, result, illustrations.chunks(result, getattr(result, 'image_id', None), 300), chat)
    assert chunks and all(p['card_id'] for p in chunks)
    assert all([decoded(await c.fetchval('SELECT refs FROM reading_cards WHERE id=$1', p['card_id'])) for p in chunks])
    assert 'SYNTHETIC' in result


@pytest.mark.parametrize('mode', ['sequential', 'verse_of_day', 'topic_of_day', 'morning_verse', 'evening_verse', 'reading_plan'])
async def test_all_scheduled_bible_modes_keep_cards(db, monkeypatch, mode):
    c, _, en, _, row = await setup(db)
    # Isolate scheduling coverage from devotional selection rules on synthetic text.
    contextual = dict(row, reading_rows=[dict(row)])
    monkeypatch.setattr('app.services.readings.daily', AsyncMock(return_value=contextual))
    monkeypatch.setattr('app.services.readings.contextual', AsyncMock(return_value=contextual))
    monkeypatch.setattr('app.services.bible.topic_verse', AsyncMock(return_value=('fixture', row)))
    monkeypatch.setattr('app.services.devotionals.selected_verse', AsyncMock(return_value=(contextual, None)))
    sub = await create_or_update_subscription(c, chat_id=101, created_by=101, translation_id=en['id'],
        mode=mode, send_time=time(9), timezone_name='UTC',
        plan_code='bible-90' if mode=='reading_plan' else None)
    await c.execute("UPDATE subscriptions SET next_run_at=now()-interval '1 minute' WHERE id=$1", sub['id'])
    delivery = await prepare_subscription(c, sub['id'], 300)
    chunks = decoded(await c.fetchval('SELECT chunks FROM delivery_log WHERE id=$1', delivery))
    assert chunks and all(p['card_id'] for p in chunks)
    assert all([await languages.available(c, await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', p['card_id'])) for p in chunks])


async def test_long_target_uses_same_message_text_pages_and_restart_retry(db, monkeypatch):
    c, settings, en, chat, row = await setup(db)
    ru, _, _ = await load_fixture(c, db[2], 'rus')
    await c.execute("UPDATE verses SET text=repeat('Очень длинный текст. ',3000) WHERE translation_id=$1 AND book_code='GEN' AND chapter=1 AND verse=1", ru['id'])
    _, original = await card(c, en, chat, row)
    edit = await languages.select(c, 101, 501, original['id'], 's', ru['id'])
    state = await c.fetchrow('SELECT * FROM reading_cards WHERE id=$1', original['id'])
    assert len(languages.pages(state['current_html'])) > 1
    await c.execute("UPDATE delivery_log SET status='sending',sending_chunk=0 WHERE id=$1", edit)
    await recover_ambiguous(c)
    assert await c.fetchval('SELECT status FROM delivery_log WHERE id=$1', edit) == 'retry'
    await c.execute('UPDATE delivery_log SET retry_at=now() WHERE id=$1', edit)
    bot, transport = sender(c, settings, monkeypatch)
    assert await process_delivery(c, edit, transport) == 'sent'
    page = await languages.select(c, 101, 501, original['id'], 't', 1)
    assert await process_delivery(c, page, transport) == 'sent'
    assert bot.edit_message_text.await_args.kwargs['message_id'] == 501
    assert '📖 2/' in str(bot.edit_message_text.await_args.kwargs['reply_markup'])
    bot.send_rich_message.assert_not_awaited()


async def test_callback_reauthorizes_private_and_group_access(db, monkeypatch):
    from aiogram.types import CallbackQuery, User
    c, settings, en, chat, row = await setup(db)
    _, original = await card(c, en, chat, row)
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, 'answer', answer)
    query = CallbackQuery(id='fixture', chat_instance='fixture', from_user=User(id=202,is_bot=False,first_name='Other'),
        message=message('source').model_copy(update={'message_id':501}), data=f"lc:{original['id']}:s:{en['id']}")
    await handlers.message_language_handler(query, None, pool_for(c), settings)
    assert answer.await_args.kwargs['show_alert'] is True
    assert await c.fetchval('SELECT count(*) FROM delivery_log') == 0
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),get_me=AsyncMock(return_value=SimpleNamespace(id=123)))
    query = query.model_copy(update={'message':message('source',chat_id=-100,chat_type='supergroup')})
    await handlers.message_language_handler(query, bot, pool_for(c), settings)
    assert answer.await_args.kwargs['show_alert'] is True
    assert await c.fetchval('SELECT count(*) FROM delivery_log') == 0
