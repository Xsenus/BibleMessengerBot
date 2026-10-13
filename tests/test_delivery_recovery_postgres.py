"""Durable fan-out intent, retry ceilings, recipient identities and actual outbox counters."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import announcements as a
from app.services import broadcast, fanout
from app.services import chat_sources as cs
from app.services import video_watch as vw
from app.services.errors import UserError
from tests.test_postgres_integration import create_destination, load_fixture
from tests.test_postgres_integration import db as db  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='needs PostgreSQL')]


class EnqueueFailure:
    """Fail the batch and one recipient, while all successful writes use real PostgreSQL."""
    def __init__(self, connection, *, fail_all=False):
        self.connection = connection
        self.fail_all = fail_all

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, query, *args, **kwargs):
        if 'FROM unnest(' in query:
            raise RuntimeError('transient batch failure')
        return await self.connection.execute(query, *args, **kwargs)

    async def fetchrow(self, query, *args, **kwargs):
        if query == 'SELECT * FROM telegram_chats WHERE telegram_chat_id=$1' and (self.fail_all or args[0] == 202):
            raise RuntimeError('transient destination failure')
        return await self.connection.fetchrow(query, *args, **kwargs)


async def audience(connection):
    await create_destination(connection, 101)
    await create_destination(connection, 202)
    await connection.execute('UPDATE telegram_users SET is_owner=true WHERE telegram_user_id=101')


async def captured_post(connection, *, mode='capture', body='Message', message_id='1'):
    source = await cs.ensure_source(connection, 'telegram', -100500, 'Source channel')
    await connection.execute('UPDATE content_sources SET is_enabled=true,mode=$2 WHERE id=$1', source['id'], mode)
    return await cs.ingest_post(connection, 'telegram', -100500, message_id, body)


async def due_post(connection, post_id):
    await connection.execute("UPDATE source_posts SET publication_retry_at=now()-interval '1 second' WHERE id=$1", post_id)


async def outbox_count(connection, key):
    return await connection.fetchval('SELECT count(*) FROM delivery_log WHERE payload_key=$1', key)


async def test_partial_announcement_retries_missing_recipient_without_duplicates(db):
    connection = db[0]
    await audience(connection)
    announcement = await a.create(connection, 101, 'Test', 'Message', a.parse_when('now'))
    with pytest.raises(RuntimeError):
        await a.run_one(EnqueueFailure(connection), announcement['id'])
    run = await connection.fetchrow('SELECT * FROM announcement_runs')
    assert run['status'] == 'sending' and run['queued'] == 1 and run['skipped'] == 0
    await connection.execute("UPDATE announcement_runs SET created_at=now()-interval '2 minutes' WHERE id=$1", run['id'])
    await a.run_due(connection)
    final = await connection.fetchrow('SELECT * FROM announcement_runs WHERE id=$1', run['id'])
    assert final['status'] == 'done' and final['recipients'] == final['queued'] == 2 and final['skipped'] == 0
    assert await outbox_count(connection, f"announcement:{run['id']}") == 2


async def test_source_partial_failure_is_recovered_and_counts_all_attempts(db):
    connection = db[0]
    await audience(connection)
    post = await captured_post(connection)
    result = await cs.publish(EnqueueFailure(connection), post)
    assert result == {'recipients': 2, 'queued': 1, 'skipped': 0, 'failed': 1}
    row = await connection.fetchrow('SELECT * FROM source_posts WHERE id=$1', post)
    assert row['status'] == 'new' and row['published_at'] is None and row['publication_requested_at'] is not None
    await due_post(connection, post)
    assert await cs.retry_publications(connection) == 1
    row = await connection.fetchrow('SELECT * FROM source_posts WHERE id=$1', post)
    assert row['status'] == 'published' and row['queued'] == row['recipients'] == 2 and row['skipped'] == 0
    assert await outbox_count(connection, f'source-post:{post}') == 2
    with pytest.raises(UserError):
        await cs.publish(connection, post)


async def test_source_retry_uses_confirmed_audience_after_configuration_changes(db):
    connection = db[0]
    await audience(connection)
    post = await captured_post(connection)
    assert (await cs.publish(EnqueueFailure(connection, fail_all=True), post, {'chat_ids': [101]}))['failed'] == 1
    await connection.execute("UPDATE content_sources SET audience='{\"chat_ids\":[202]}'::jsonb")
    await due_post(connection, post)
    assert await cs.retry_publications(connection) == 1
    destinations = await connection.fetch('SELECT telegram_chat_id FROM delivery_log WHERE payload_key=$1', f'source-post:{post}')
    assert [row['telegram_chat_id'] for row in destinations] == [101]


@pytest.mark.parametrize('mode', ['capture', 'approve'])
async def test_source_recovery_never_publishes_unapproved_posts(db, mode):
    connection = db[0]
    await audience(connection)
    post = await captured_post(connection, mode=mode)
    assert await cs.retry_publications(connection) == 0
    assert await connection.fetchval('SELECT status FROM source_posts WHERE id=$1', post) == 'new'
    assert await outbox_count(connection, f'source-post:{post}') == 0


async def test_crash_between_post_confirmation_and_enqueue_is_recovered(db, monkeypatch):
    connection = db[0]
    await audience(connection)
    post = await captured_post(connection)
    with monkeypatch.context() as patch:
        patch.setattr(cs, 'deliver_post', AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await cs.publish(connection, post)
    assert await connection.fetchval('SELECT publication_requested_at IS NOT NULL FROM source_posts WHERE id=$1', post)
    assert await outbox_count(connection, f'source-post:{post}') == 0
    assert await cs.retry_publications(connection) == 1
    assert await outbox_count(connection, f'source-post:{post}') == 2


async def test_post_retry_is_bounded_and_manual_retry_preserves_existing_jobs(db):
    connection = db[0]
    await audience(connection)
    post = await captured_post(connection)
    flaky = EnqueueFailure(connection)
    await cs.publish(flaky, post)
    for _ in range(4):
        await due_post(connection, post)
        assert await cs.retry_publications(flaky) == 0
    assert await connection.fetchval('SELECT publication_attempts FROM source_posts WHERE id=$1', post) == 5
    assert await cs.retry_publications(connection) == 0
    assert (await cs.publish(connection, post))['queued'] == 2
    assert await outbox_count(connection, f'source-post:{post}') == 2


@pytest.mark.parametrize('platform', ['telegram', 'max', 'vk'])
async def test_source_excludes_registered_origin_using_platform_identity(db, platform):
    connection = db[0]
    await create_destination(connection, 101)
    external = -100500
    internal = external if platform == 'telegram' else -4_000_000_000_000_001
    await create_destination(connection, internal)
    await connection.execute('UPDATE telegram_chats SET platform=$2 WHERE telegram_chat_id=$1', internal, platform)
    if platform != 'telegram':
        await connection.execute("INSERT INTO platform_identities(platform,kind,external_id,internal_id) VALUES($1,'chat',$2,$3)",
                                 platform, external, internal)
        # An equal Telegram ID must remain in the audience; only the bridge source is excluded.
        await create_destination(connection, external)
    source = await cs.ensure_source(connection, platform, external, 'Source')
    assert source['chat_id'] == internal
    await connection.execute("UPDATE content_sources SET is_enabled=true,mode='capture',chat_id=NULL WHERE id=$1", source['id'])
    post = await cs.ingest_post(connection, platform, external, '1', 'Post')
    await cs.publish(connection, post)
    destinations = {row['telegram_chat_id'] for row in await connection.fetch('SELECT telegram_chat_id FROM delivery_log WHERE payload_key=$1',
                                                                            f'source-post:{post}')}
    assert internal not in destinations and 101 in destinations
    if platform != 'telegram':
        assert external in destinations


async def test_parallel_publication_recovery_creates_no_duplicates(db):
    connection, settings, _ = db
    await audience(connection)
    post = await captured_post(connection)
    await cs.publish(EnqueueFailure(connection), post)
    await due_post(connection, post)
    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=2)
    try:
        async def recover():
            async with pool.acquire() as worker:
                return await cs.retry_publications(worker)
        await asyncio.gather(recover(), recover())
    finally:
        await pool.close()
    assert await outbox_count(connection, f'source-post:{post}') == 2
    assert await connection.fetchval('SELECT status FROM source_posts WHERE id=$1', post) == 'published'


async def prepare_broadcast(db):
    connection, _, tmp = db
    await audience(connection)
    edition, _, _ = await load_fixture(connection, tmp)
    await connection.execute('UPDATE telegram_chats SET default_translation_id=$1', edition['id'])
    identifier = await broadcast.offer(connection, 101, [dict(book='GEN', chapter=1, first=1, last=1)], edition['id'])
    return identifier


async def test_cancelled_broadcast_is_resumed_from_committed_confirmation(db, monkeypatch):
    connection = db[0]
    identifier = await prepare_broadcast(db)
    with monkeypatch.context() as patch:
        patch.setattr(broadcast, 'recipients', AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await broadcast.send(connection, identifier, 101)
    assert await connection.fetchval('SELECT status FROM broadcasts WHERE id=$1', identifier) == 'sending'
    await connection.execute("UPDATE broadcasts SET retry_at=now()-interval '1 second' WHERE id=$1", identifier)
    assert await broadcast.recover(connection) == 1
    assert await outbox_count(connection, f'broadcast:{identifier}') == 2
    assert await connection.fetchval('SELECT status FROM broadcasts WHERE id=$1', identifier) == 'done'
    assert await connection.fetchval("SELECT count(*) FROM operator_events WHERE action='broadcast'") == 1


async def test_partial_broadcast_recovers_without_duplicate_jobs_or_completion_event(db):
    connection = db[0]
    identifier = await prepare_broadcast(db)
    with pytest.raises(RuntimeError):
        await broadcast.send(EnqueueFailure(connection), identifier, 101)
    assert await outbox_count(connection, f'broadcast:{identifier}') == 1
    await connection.execute("UPDATE broadcasts SET retry_at=now()-interval '1 second' WHERE id=$1", identifier)
    assert await broadcast.recover(connection) == 1
    row = await connection.fetchrow('SELECT * FROM broadcasts WHERE id=$1', identifier)
    assert row['status'] == 'done' and row['queued'] == row['recipients'] == 2 and row['skipped'] == 0
    assert await connection.fetchval("SELECT count(*) FROM operator_events WHERE action='broadcast'") == 1


async def test_broadcast_retries_poisoned_payload_at_most_five_times(db, monkeypatch):
    connection = db[0]
    identifier = await prepare_broadcast(db)
    with monkeypatch.context() as patch:
        patch.setattr(broadcast, 'recipients', AsyncMock(side_effect=ValueError('bad audience')))
        with pytest.raises(ValueError):
            await broadcast.send(connection, identifier, 101)
        for _ in range(4):
            await connection.execute("UPDATE broadcasts SET retry_at=now()-interval '1 second' WHERE id=$1", identifier)
            assert await broadcast.recover(connection) == 0
        assert await broadcast.recover(connection) == 0
    row = await connection.fetchrow('SELECT * FROM broadcasts WHERE id=$1', identifier)
    assert row['status'] == 'failed' and row['attempts'] == 5 and row['last_error'] == 'ValueError'
    assert (await broadcast.send(connection, identifier, 101))['queued'] == 2
    assert await outbox_count(connection, f'broadcast:{identifier}') == 2


async def new_video(connection, monkeypatch):
    await audience(connection)
    feed = await vw.add(connection, 'youtube', 'UC' + 'a' * 22, notify=True)
    await connection.execute('UPDATE video_feeds SET baselined=true WHERE id=$1', feed['id'])
    feed = await connection.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
    item = dict(id='abcdefghijk', title='New video', url='https://www.youtube.com/watch?v=abcdefghijk',
                published=datetime.now(UTC) + timedelta(seconds=1))
    monkeypatch.setattr(vw, 'fetch', AsyncMock(return_value=[item]))
    return feed


@pytest.mark.parametrize('fail_all', [False, True])
async def test_video_enqueue_failure_retries_even_after_video_leaves_rss(db, monkeypatch, fail_all):
    connection = db[0]
    feed = await new_video(connection, monkeypatch)
    assert await vw.poll_feed(EnqueueFailure(connection, fail_all=fail_all), feed, None) == 0
    item = await connection.fetchrow('SELECT * FROM video_items')
    assert item['status'] == 'new' and item['notification_attempts'] == 1
    monkeypatch.setattr(vw, 'fetch', AsyncMock(return_value=[]))
    assert await vw.poll_feed(connection, feed, None) == 1
    assert await vw.poll_feed(connection, feed, None) == 0
    assert await outbox_count(connection, f"video:{item['id']}") == 2
    assert await connection.fetchval('SELECT status FROM video_items') == 'notified'


async def test_video_malformed_enqueue_stops_after_five_attempts_and_can_resume(db, monkeypatch):
    connection = db[0]
    feed = await new_video(connection, monkeypatch)
    with monkeypatch.context() as patch:
        failing = AsyncMock(side_effect=ValueError('malformed payload'))
        patch.setattr(fanout, 'enqueue', failing)
        for _ in range(6):
            assert await vw.poll_feed(connection, feed, None) == 0
        assert failing.await_count == 5
    row = await connection.fetchrow('SELECT * FROM video_items')
    assert row['status'] == 'new' and row['notification_attempts'] == 5 and row['notification_error'] == 'ValueError'
    state = await connection.fetchrow('SELECT * FROM video_feeds WHERE id=$1', feed['id'])
    assert state['error_count'] >= 1 and 'five attempts' in state['last_error']
    await vw._change(connection, 'retry', feed['id'])
    assert await vw.poll_feed(connection, feed, None) == 1
    assert await outbox_count(connection, f"video:{row['id']}") == 2


async def test_composer_recovers_confirmed_intent_but_keeps_draft_and_unapproved_post(db):
    from app.worker import composer

    connection = db[0]
    identifier = await prepare_broadcast(db)
    unconfirmed_broadcast = await broadcast.offer(connection, 101, [dict(book='GEN', chapter=1, first=1, last=1)], None)
    post = await captured_post(connection)
    unapproved_post = await captured_post(connection, message_id='2')
    await cs.publish(EnqueueFailure(connection), post)
    with pytest.raises(RuntimeError):
        await broadcast.send(EnqueueFailure(connection), identifier, 101)
    await due_post(connection, post)
    await connection.execute("UPDATE broadcasts SET retry_at=now()-interval '1 second' WHERE id=$1", identifier)
    await composer.recover_delivery(connection)
    assert await outbox_count(connection, f'source-post:{post}') == 2
    assert await outbox_count(connection, f'broadcast:{identifier}') == 2
    assert await outbox_count(connection, f'source-post:{unapproved_post}') == 0
    assert await outbox_count(connection, f'broadcast:{unconfirmed_broadcast}') == 0
    assert await connection.fetchval('SELECT status FROM source_posts WHERE id=$1', unapproved_post) == 'new'
    assert await connection.fetchval('SELECT status FROM broadcasts WHERE id=$1', unconfirmed_broadcast) == 'draft'


async def test_retry_counts_previously_queued_destinations_even_if_new_attempt_errors(db):
    connection = db[0]
    await audience(connection)
    first = await fanout.enqueue(connection, key='same-key', text='Message', mode='announcement')
    second = await fanout.enqueue(EnqueueFailure(connection), key='same-key', text='Message', mode='announcement')
    assert first == second == {'recipients': 2, 'queued': 2, 'skipped': 0, 'failed': 0}
    assert await outbox_count(connection, 'same-key') == 2
