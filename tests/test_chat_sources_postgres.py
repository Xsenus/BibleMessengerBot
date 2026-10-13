"""Источники постов (группы и каналы): режимы, дедупликация, защита адресов картинок, публикация копий."""
import os

import pytest

from app.services import chat_sources as cs
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')


def test_safe_image_url():
    assert cs.safe_image_url('https://example.com/a.png')
    for url in ('http://example.com/a.png', 'https://127.0.0.1/a.png', 'https://10.0.0.1/a', 'https://localhost/a',
                'https://user@example.com/a', 'https://example.com:8443/a', 'https://x.internal/a'):
        assert not cs.safe_image_url(url)


def test_mime_detection():
    assert cs.mime_of(b'\x89PNG\r\n\x1a\n' + b'0' * 10) == 'image/png'
    assert cs.mime_of(b'plain text') is None


async def prepare(c, mode):
    await create_destination(c, 101)
    await create_destination(c, 202)
    await c.execute("UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101")
    source = await cs.ensure_source(c, 'telegram', -100500, 'Канал')
    await c.execute('UPDATE content_sources SET is_enabled=true, mode=$2, min_length=1 WHERE id=$1', source['id'], mode)


@pytest.mark.asyncio
async def test_unknown_source_is_registered_disabled_and_ignored(db):
    c = db[0]
    assert await cs.ingest_post(c, 'telegram', -100777, '1', 'текст поста') is None
    assert await c.fetchval('SELECT is_enabled FROM content_sources WHERE external_id=-100777') is False
    assert await c.fetchval('SELECT count(*) FROM source_posts') == 0


@pytest.mark.asyncio
async def test_capture_mode_stores_once_and_publish_is_idempotent(db):
    c = db[0]
    await prepare(c, 'capture')
    post = await cs.ingest_post(c, 'telegram', -100500, '10', 'Слово дня')
    assert post and await cs.ingest_post(c, 'telegram', -100500, '10', 'Слово дня') is None
    assert await c.fetchval('SELECT count(*) FROM delivery_log WHERE mode=\'source_post\'') == 0
    counts = await cs.publish(c, post)
    assert counts['queued'] >= 1
    with pytest.raises(UserError):
        await cs.publish(c, post)
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='source_post'") == counts['queued']


@pytest.mark.asyncio
async def test_auto_mode_publishes_immediately_and_approve_notifies_admin(db):
    c = db[0]
    await prepare(c, 'auto')
    await cs.ingest_post(c, 'telegram', -100500, '1', 'Авто-пост')
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='source_post'") >= 1
    await c.execute("UPDATE content_sources SET mode='approve'")
    post = await cs.ingest_post(c, 'telegram', -100500, '2', 'Нужно одобрение')
    assert await c.fetchval("SELECT status FROM source_posts WHERE id=$1", post) == 'new'
    assert await c.fetchval("SELECT count(*) FROM delivery_log WHERE mode='announcement' AND telegram_chat_id=101") == 1


@pytest.mark.asyncio
async def test_post_command_requires_admin(db):
    c = db[0]
    await prepare(c, 'capture')
    post = await cs.ingest_post(c, 'telegram', -100500, '5', 'Текст')
    with pytest.raises(UserError):
        await cs.command(c, 202, [str(post)])
    assert 'Копия' in await cs.command(c, 101, [str(post)])
    assert 'не найден' in (await cs.command(c, 101, ['ignore', '999999'])).lower()
