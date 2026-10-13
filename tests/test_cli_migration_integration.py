"""Migration CLI against an isolated PostgreSQL DB, never the configured database."""
import os
from dataclasses import replace
import shutil
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip('asyncpg')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    os.getenv('RUN_DB_TESTS') != '1', reason='Requires an isolated PostgreSQL test server')]

from app import cli
from app.config import Settings
from app.db import apply_schema, migration_files, migration_status
from app.services.seed import seed_static_content


async def test_migrate_preserves_references_and_operator_changes(tmp_path, monkeypatch):
    original = os.environ['DATABASE_URL']
    name = 'biblebot_cli_test_' + uuid4().hex
    dsn = urlunsplit(urlsplit(original)._replace(path='/' + name))
    settings = replace(Settings.from_env(require_bot_token=False), database_url=dsn, bible_profile='none')
    admin = await asyncpg.connect(original)
    connection = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        # Start one real migration behind the release, using identical file bytes.
        schema = tmp_path / 'sql' / 'schema.sql'
        schema.parent.mkdir()
        (schema.parent / 'migrations').mkdir()
        files = migration_files()
        for source in files[:-1]:
            destination = schema if source.name == 'schema.sql' else schema.parent / 'migrations' / source.name
            shutil.copyfile(source, destination)
        await apply_schema(settings, schema_path=schema)
        connection = await asyncpg.connect(dsn)
        await seed_static_content(connection)
        await connection.execute("UPDATE topics SET title_ru='Настройка оператора',is_active=false WHERE code=(SELECT min(code) FROM topics)")
        await connection.execute("UPDATE topic_references SET weight=57 WHERE topic_code=(SELECT min(code) FROM topics)")
        references_sql = 'SELECT * FROM topic_references ORDER BY topic_code,book_code,chapter,verse_from,verse_to'
        before = await connection.fetch(references_sql)
        topics_before = await connection.fetch('SELECT * FROM topics ORDER BY code')
        assert (await migration_status(settings))['pending'] == [files[-1].name]
        monkeypatch.setattr(cli.Settings, 'from_env', lambda **kwargs: settings)

        result = await cli.run(cli.parser().parse_args(['migrate']))

        assert result['status'] == 'ok' and result['applied'] == len(files)
        assert await connection.fetch(references_sql) == before
        assert await connection.fetch('SELECT * FROM topics ORDER BY code') == topics_before
        # A replay and the read-only check must leave those same rows untouched.
        assert (await cli.run(cli.parser().parse_args(['migrate']))) == result
        assert (await cli.run(cli.parser().parse_args(['migrate', '--check']))) == result
        assert await connection.fetch(references_sql) == before
        assert await connection.fetch('SELECT * FROM topics ORDER BY code') == topics_before
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
