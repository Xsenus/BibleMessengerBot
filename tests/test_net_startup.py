"""Cold starts must use persisted routes before their first external API call."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.net.router import ROUTER, Policy, Route


class ProbeComplete(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('service', ['telegram', 'max', 'vk'])
async def test_startup_loads_saved_proxy_before_first_api_request(monkeypatch, service):
    from app.bot import main as telegram
    from app.maxbot import main as maxbot
    from app.vkbot import main as vkbot

    module = {'telegram': telegram, 'max': maxbot, 'vk': vkbot}[service]
    owner = SimpleNamespace(fetchval=AsyncMock(return_value=True))

    @asynccontextmanager
    async def lease():
        yield owner

    saved = Route('proxy:42', 'Saved route', 'socks5://127.0.0.1:1080')
    load = AsyncMock(return_value={service: Policy('failover', 1, [saved])})
    refresh_loop = AsyncMock()

    class Client:
        def __init__(self, *args, session=None, **kwargs):
            self.session = session or SimpleNamespace(close=AsyncMock())

        async def first_request(self):
            assert ROUTER.plan(service) == [saved]
            load.assert_awaited_once_with(owner)
            refresh_loop.assert_not_called()
            raise ProbeComplete

        get_webhook_info = first_request
        get_me = first_request

        async def close(self):
            await self.session.close()

    class Dispatcher(dict):
        def include_router(self, *args):
            pass

    monkeypatch.setattr(module.Settings, 'from_env', lambda **kwargs: SimpleNamespace(bot_token='123456:' + 'A' * 35))
    monkeypatch.setattr(module, 'wait_for_database', AsyncMock())
    monkeypatch.setattr(module, 'create_pool', AsyncMock(return_value=SimpleNamespace(acquire=Mock(side_effect=lease))))
    monkeypatch.setattr(module, 'close_pool', AsyncMock())
    monkeypatch.setattr(module, 'acquire_runtime_guard', AsyncMock())
    monkeypatch.setattr(module.net_store, 'load', load)
    monkeypatch.setattr(module.net_store, 'refresh_loop', refresh_loop)
    if service == 'telegram':
        monkeypatch.setattr(module, 'Bot', Client)
        monkeypatch.setattr(module, 'Dispatcher', Dispatcher)
    else:
        config_class = module.MaxSettings if service == 'max' else module.VkSettings
        monkeypatch.setattr(config_class, 'from_env', lambda **kwargs: SimpleNamespace(
            webhook_url='https://fixture.invalid/max', webhook_secret='fixture', group_id=1))
        monkeypatch.setattr(module, 'MaxClient' if service == 'max' else 'VkClient', Client)
    ROUTER.set_policies({})
    try:
        with pytest.raises(ProbeComplete):
            await module.main()
    finally:
        ROUTER.set_policies({})
