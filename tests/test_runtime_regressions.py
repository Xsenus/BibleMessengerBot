"""Runtime regressions: lost lock sessions, unavailable plans, and authentication."""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services.accounts import claim_owner
from app.services.errors import UserError
from app.services.plans import validate_plan


@pytest.mark.asyncio
async def test_lost_singleton_connection_stops_polling(monkeypatch):
    import app.bot.main as main

    owner = SimpleNamespace(
        fetchval=AsyncMock(return_value=True),
        execute=AsyncMock(side_effect=ConnectionError('fixture lost lock session')),
    )

    @asynccontextmanager
    async def lease():
        yield owner

    pool = SimpleNamespace(acquire=Mock(side_effect=lease))
    bot = SimpleNamespace(
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        set_my_commands=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    stopped = asyncio.Event()

    class Dispatcher(dict):
        def include_router(self, router):
            pass

        def resolve_used_update_types(self):
            return ['message']

        async def start_polling(self, *args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    monkeypatch.setattr(main.Settings, 'from_env', lambda: SimpleNamespace(bot_token='fixture'))
    monkeypatch.setattr(main, 'wait_for_database', AsyncMock())
    monkeypatch.setattr(main, 'create_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(main, 'close_pool', AsyncMock())
    monkeypatch.setattr(main, 'Bot', lambda *args, **kwargs: bot)
    monkeypatch.setattr(main, 'Dispatcher', Dispatcher)
    async def branding_loop(*args):
        await asyncio.Future()
    monkeypatch.setattr(main, 'branding_loop', branding_loop)
    monkeypatch.setattr(main, 'reconciliation_loop', branding_loop)

    with pytest.raises(ConnectionError, match='lost lock session'):
        await main.main()

    assert pool.acquire.call_count == 1
    owner.execute.assert_awaited_once()
    assert stopped.is_set()
    bot.session.close.assert_awaited_once()
    main.close_pool.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('chapters', [0, 6, 149])
async def test_psalm_plan_rejected_before_schedule_for_insufficient_chapters(chapters):
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=chapters))
    edition = {'id': 1, 'canonical_66_complete': False, 'nt_complete': True}
    with pytest.raises(UserError, match='invalid'):
        await validate_plan(connection, edition, 'psalms-150')


@pytest.mark.asyncio
async def test_full_psalm_plan_accepts_book_only_edition():
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=150))
    edition = {'id': 1, 'canonical_66_complete': False, 'nt_complete': False}
    await validate_plan(connection, edition, 'psalms-150')


@pytest.mark.asyncio
async def test_unicode_owner_claim_rejected_without_database_access():
    assert not await claim_owner(None, telegram_user_id=1,
        supplied_code='неверный код', expected_code='a' * 32)


def test_unicode_admin_key_rejected_without_server_error(monkeypatch):
    import app.web.main as web
    monkeypatch.setattr(web, 'settings', replace(web.settings, admin_api_key='a' * 32))
    assert not web.valid_key('неверный ключ')
    assert web.valid_key('a' * 32)


@pytest.mark.asyncio
async def test_operator_endpoints_require_auth_and_escape_dashboard(monkeypatch):
    import app.web.main as web
    monkeypatch.setattr(web, 'settings', replace(web.settings, admin_api_key='a' * 32))
    data = AsyncMock(return_value={'fixture': '<script>alert(1)</script>'})
    monkeypatch.setattr(web, 'operator_data', data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
            base_url='http://fixture.invalid') as client:
        assert (await client.get('/api/stats')).status_code == 401
        assert (await client.get('/admin')).status_code == 401
        data.assert_not_awaited()
        response = await client.get('/admin', auth=('admin', 'a' * 32))
        assert response.status_code == 200
        assert '<script>' not in response.text and '&lt;script&gt;' in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        response = await client.get('/api/stats', headers={'X-Admin-Key': 'a' * 32})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_liveness_does_not_mask_missing_database(monkeypatch):
    import app.web.main as web
    monkeypatch.setattr(web, 'create_pool', AsyncMock(side_effect=ConnectionError('fixture')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
            base_url='http://fixture.invalid') as client:
        assert (await client.get('/health')).status_code == 200
        response = await client.get('/ready')
        assert response.status_code == 503
        assert response.json() == {'detail': 'Database unavailable'}


@pytest.mark.asyncio
@pytest.mark.parametrize('chat_id', [101, -100101])
async def test_status_resolution_commands_are_copyable_in_private_and_group_chat(monkeypatch, chat_id):
    from app.bot import handlers
    from app.bot.commands import parse_command
    monkeypatch.setattr(handlers, 'settings_text', AsyncMock(return_value='fixture'))
    monkeypatch.setattr(handlers, 'list_subscriptions', AsyncMock(return_value=[]))
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[{
        'id': 7, 'status': 'uncertain', 'next_chunk': 0, 'total': 1,
    }]))
    text = await handlers.status_text(connection, {'telegram_chat_id': chat_id, 'ui_language': 'en'})
    commands = re.findall(r'<code>(/resolve.*?)</code>', text)
    assert len(commands) == 3
    for command in commands:
        parsed = parse_command(command)
        assert len(parsed.arguments) == 2
        assert parsed.arguments[0] == '7'
        assert parsed.target == (str(chat_id) if chat_id < 0 else None)


def test_container_start_probe_accounts_for_kernel_rounding(tmp_path, monkeypatch):
    from app import healthcheck
    (tmp_path / '1').mkdir()
    fields = ['0'] * 20
    fields[19] = '250'
    (tmp_path / '1' / 'stat').write_text('1 (name with ) parentheses) ' + ' '.join(fields))
    (tmp_path / 'stat').write_text('cpu 1 2 3\nbtime 1000\n')
    monkeypatch.setattr(healthcheck.os, 'sysconf', lambda name: 100, raising=False)
    assert healthcheck.container_started_at(tmp_path) == datetime.fromtimestamp(1003.5, timezone.utc)


@pytest.mark.asyncio
async def test_health_probe_fails_closed_without_container_identity(monkeypatch):
    from app import healthcheck
    monkeypatch.setattr(healthcheck, 'container_started_at', lambda: None)
    assert not await healthcheck.probe('bot')
    assert not await healthcheck.probe('unknown')


@pytest.mark.asyncio
async def test_cli_bootstrap_passes_explicit_profile_and_edition_limit(monkeypatch):
    from app import cli
    bootstrap = AsyncMock(return_value={'schema': 'applied'})
    monkeypatch.setattr(cli, 'bootstrap', bootstrap)
    result = await cli.run(cli.parser().parse_args(['bootstrap', '--profile', 'none', '--max-editions', '7']))
    assert result == {'schema': 'applied'}
    settings = bootstrap.await_args.args[0]
    assert settings.bible_profile == 'none' and settings.max_editions_per_language == 7


@pytest.mark.asyncio
async def test_bootstrap_keeps_caller_settings(monkeypatch):
    from app import bootstrap
    from app.config import Settings
    settings = replace(Settings.from_env(require_bot_token=False), bible_profile='none')

    @asynccontextmanager
    async def maintenance(value):
        assert value is settings
        yield None

    monkeypatch.setattr(bootstrap, 'wait_for_database', AsyncMock())
    monkeypatch.setattr(bootstrap, 'maintenance', maintenance)
    locked = AsyncMock(return_value={'schema': 'applied'})
    monkeypatch.setattr(bootstrap, 'bootstrap_locked', locked)
    await bootstrap.bootstrap(settings)
    locked.assert_awaited_once_with(settings)
