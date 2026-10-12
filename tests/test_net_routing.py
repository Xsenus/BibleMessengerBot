"""Сетевые маршруты: выбор пути, автоматы отключения, переключение при сбоях, сессия Telegram и панель."""

import aiohttp
import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError

from app.net.router import DIRECT, OPEN_AFTER, Policy, Route, Router
from app.net.services import service_for_host
from app.net.store import build_url, parse_proxy_url
from app.net.telegram import RoutedSession
from app.net.transport import SmartTransport

P1 = Route('proxy:1', 'Прокси 1', 'http://p1:3128')
P2 = Route('proxy:2', 'Прокси 2', 'socks5://p2:1080')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def router(strategy='failover', routes=(DIRECT, P1, P2), attempts=3):
    clock = Clock()
    r = Router(clock)
    r.set_policies({'telegram': Policy(strategy, attempts, list(routes))})
    return r, clock


def test_failover_keeps_order_and_demotes_open_routes():
    r, clock = router()
    assert [x.key for x in r.plan('telegram')] == ['direct', 'proxy:1', 'proxy:2']
    for _ in range(OPEN_AFTER):
        r.record_failure('telegram', DIRECT, 'ConnectError')
    assert [x.key for x in r.plan('telegram')] == ['proxy:1', 'proxy:2', 'direct']       # сломанный путь — в конец, но не потерян
    clock.now += 31
    assert r.plan('telegram')[0].key == 'direct'                                          # пауза истекла — пробуем снова
    r.record_success('telegram', DIRECT, 50)
    assert r.state('telegram', DIRECT).consecutive == 0


def test_pause_grows_and_is_capped():
    r, clock = router()
    for _ in range(OPEN_AFTER + 10):
        r.record_failure('telegram', P1, 'x')
    assert r.state('telegram', P1).open_until - clock.now == 900


def test_auto_prefers_fast_and_reliable():
    r, _ = router('auto')
    r.record_success('telegram', DIRECT, 900)
    r.record_success('telegram', P1, 80)
    r.record_success('telegram', P2, 300)
    assert [x.key for x in r.plan('telegram')] == ['proxy:1', 'proxy:2', 'direct']
    r.record_failure('telegram', P1, 'x')
    r.record_failure('telegram', P1, 'x')
    assert r.plan('telegram')[0].key == 'proxy:2'


def test_round_robin_rotates_among_healthy_only():
    r, _ = router('round_robin')
    firsts = [r.plan('telegram')[0].key for _ in range(4)]
    assert firsts == ['direct', 'proxy:1', 'proxy:2', 'direct']
    for _ in range(OPEN_AFTER):
        r.record_failure('telegram', P1, 'x')
    assert 'proxy:1' not in {r.plan('telegram')[0].key for _ in range(6)}


def test_all_routes_broken_still_tries_the_soonest_to_reopen_and_attempts_are_limited():
    r, clock = router(attempts=2)
    for route in (DIRECT, P1, P2):
        for _ in range(OPEN_AFTER):
            r.record_failure('telegram', route, 'x')
        clock.now += 1
    plan = r.plan('telegram')
    assert len(plan) == 2 and plan[0].key == 'direct'


def test_unconfigured_service_uses_direct():
    assert [x.key for x in Router().plan('vk')] == ['direct']


def test_service_detection_by_host():
    cases = {'api.telegram.org': 'telegram', 'platform-api2.max.ru': 'max', 'api.vk.com': 'vk', 'pu.vk.com': 'vk', 'www.youtube.com': 'youtube',
             'rutube.ru': 'rutube', 'api.openai.com': 'images', 'example.org': 'sources'}
    assert {h: service_for_host(h) for h in cases} == cases


def test_proxy_url_parsing_and_building():
    assert parse_proxy_url('socks5://user:pw@10.0.0.1:1080') == ('socks5', '10.0.0.1:1080')
    for bad in ('ftp://h:1', 'http://h', 'http://h:99999', 'http://h:3128/path', 'nonsense', 'http://:3128'):
        with pytest.raises(ValueError):
            parse_proxy_url(bad)
    assert build_url('http', 'h', 3128, 'us er', 'p@ss') == 'http://us%20er:p%40ss@h:3128'


# ----- транспорт --------------------------------------------------------------------------------------------------

def smart(r, handlers):
    transport = SmartTransport('telegram', router=r)
    calls = []

    def pick(route, request):
        def handler(req):
            calls.append(route.key)
            return handlers[route.key](req)
        return httpx.MockTransport(handler)
    transport._transport = pick                                              # type: ignore[method-assign]
    return transport, calls


def boom_connect(request):
    raise httpx.ConnectError('down')


@pytest.mark.asyncio
async def test_connect_error_switches_route_for_post_and_records_health():
    r, _ = router()
    transport, calls = smart(r, {'direct': boom_connect, 'proxy:1': lambda q: httpx.Response(200, json={'ok': True}), 'proxy:2': boom_connect})
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post('https://api.telegram.org/x', json={})
    assert response.status_code == 200 and calls == ['direct', 'proxy:1']
    assert r.state('telegram', DIRECT).failures == 1 and r.state('telegram', P1).successes == 1


@pytest.mark.asyncio
async def test_read_timeout_on_post_is_not_replayed_but_get_is():
    r, _ = router()

    def slow(request):
        raise httpx.ReadTimeout('slow')
    transport, calls = smart(r, {'direct': slow, 'proxy:1': lambda q: httpx.Response(200), 'proxy:2': slow})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post('https://api.telegram.org/x', json={})
        assert calls == ['direct']                                           # POST не дублируется: исход неизвестен
        calls.clear()
        assert (await client.get('https://api.telegram.org/x')).status_code == 200
        assert calls == ['direct', 'proxy:1']                                # GET безопасно повторить


@pytest.mark.asyncio
async def test_proxy_auth_failure_moves_on_and_http_errors_are_not_route_failures():
    r, _ = router(routes=(P1, DIRECT))
    transport, calls = smart(r, {'proxy:1': lambda q: httpx.Response(407), 'direct': lambda q: httpx.Response(500)})
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get('https://api.telegram.org/x')
    assert response.status_code == 500 and calls == ['proxy:1', 'direct']     # 500 сервиса — не вина маршрута
    assert r.state('telegram', DIRECT).failures == 0 and r.state('telegram', P1).failures == 1


@pytest.mark.asyncio
async def test_all_routes_failing_raises_last_error():
    r, _ = router()
    transport, _calls = smart(r, {k: boom_connect for k in ('direct', 'proxy:1', 'proxy:2')})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get('https://api.telegram.org/x')


@pytest.mark.asyncio
async def test_service_chosen_by_request_host_when_not_fixed():
    r = Router()
    r.set_policies({'vk': Policy('failover', 2, [P1, DIRECT]), 'telegram': Policy('failover', 2, [DIRECT])})
    transport = SmartTransport(None, router=r)
    used = []
    transport._transport = lambda route, request: httpx.MockTransport(lambda q: used.append(route.key) or httpx.Response(200))   # type: ignore[method-assign]
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get('https://api.vk.com/x')
        await client.get('https://api.telegram.org/x')
    assert used == ['proxy:1', 'direct']


# ----- сессия Telegram ---------------------------------------------------------------------------------------------

class ConnectFail(aiohttp.ClientConnectorError):
    def __init__(self):
        pass


class FakeSession:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def make_request(self, bot, method, timeout=None):
        self.calls += 1
        return self.behaviour(method)

    async def close(self):
        pass


class FakeMethod:
    def __init__(self, name):
        self.__api_method__ = name


def telegram_session(r, behaviours):
    session = RoutedSession('telegram', router=r)
    fakes = {key: FakeSession(b) for key, b in behaviours.items()}
    session._child = lambda route: fakes[route.key]                          # type: ignore[method-assign]
    return session, fakes


def network_error(cause):
    error = TelegramNetworkError(method=FakeMethod('sendMessage'), message='x')
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
async def test_telegram_switches_route_only_when_message_surely_not_sent():
    r, _ = router()

    def raise_connect(method):
        raise network_error(ConnectFail())

    def raise_timeout(method):
        raise network_error(TimeoutError())
    session, fakes = telegram_session(r, {'direct': raise_connect, 'proxy:1': lambda m: 'sent', 'proxy:2': raise_connect})
    assert await session.make_request(None, FakeMethod('sendMessage')) == 'sent' and fakes['direct'].calls == 1
    session, fakes = telegram_session(r, {'direct': raise_timeout, 'proxy:1': lambda m: 'sent', 'proxy:2': raise_connect})
    with pytest.raises(TelegramNetworkError):
        await session.make_request(None, FakeMethod('sendMessage'))
    assert fakes['proxy:1'].calls == 0                                       # таймаут отправки: дубль недопустим
    assert await session.make_request(None, FakeMethod('getUpdates')) == 'sent'     # чтение безопасно повторить
