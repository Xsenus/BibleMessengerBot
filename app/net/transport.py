"""HTTP-транспорт с выбором маршрута и автоматическим переключением, а также фабрика клиентов `routed_client`."""
from __future__ import annotations

import ssl
import time
from typing import Any

import httpx

from app.net.router import DIRECT, ROUTER, Route, Router
from app.net.services import service_for_host

IDEMPOTENT = {'GET', 'HEAD', 'OPTIONS'}
# Ошибки до установления соединения: запрос заведомо не дошёл до сервиса, поэтому его можно повторить другим путём.
CONNECT_PHASE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


class SmartTransport(httpx.AsyncBaseTransport):
    """Отправляет каждый запрос по лучшему маршруту сервиса; при сбое соединения пробует следующий.

    Изменяющие запросы (POST и др.) повторяются другим путём только при ошибках установления соединения; при таймауте
    чтения результат неизвестен, и запрос не дублируется — ошибка возвращается вызывающему коду (он решает, что делать)."""

    def __init__(self, service: str | None = None, *, router: Router = ROUTER, verify: ssl.SSLContext | str | bool = True) -> None:
        self.service, self.router, self.verify = service, router, verify
        self._proxies: dict[str, httpx.AsyncBaseTransport] = {}
        self._direct = httpx.AsyncClient(verify=verify, trust_env=True)   # прямой путь учитывает HTTPS_PROXY из окружения

    def _transport(self, route: Route, request: httpx.Request) -> httpx.AsyncBaseTransport:
        """Транспорт маршрута (создаётся лениво и переиспользуется)."""
        if route.proxy_url is None:
            return self._direct._transport_for_url(request.url)    # noqa: SLF001 — стабильный внутренний метод httpx 0.28
        cached = self._proxies.get(route.key)
        if cached is None or getattr(cached, '_proxy_url', None) != route.proxy_url:
            cached = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(route.proxy_url), verify=self.verify)
            cached._proxy_url = route.proxy_url
            self._proxies[route.key] = cached
        return cached

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Выполнить запрос по плану маршрутов, записывая успехи и неудачи в роутер."""
        service = self.service or service_for_host(request.url.host)
        plan = self.router.plan(service) or [DIRECT]
        last: Exception | None = None
        for route in plan:
            started = time.monotonic()
            try:
                response = await self._transport(route, request).handle_async_request(request)
            except httpx.TransportError as error:
                self.router.record_failure(service, route, type(error).__name__)
                last = error
                if isinstance(error, CONNECT_PHASE) or request.method in IDEMPOTENT:
                    continue
                raise
            if response.status_code == 407 and route.proxy_url is not None:       # прокси не принял логин/пароль
                self.router.record_failure(service, route, 'proxy_auth')
                await response.aclose()
                last = httpx.ProxyError('Proxy authentication required')
                continue
            self.router.record_success(service, route, (time.monotonic() - started) * 1000)
            return response
        assert last is not None
        raise last

    async def aclose(self) -> None:
        """Закрыть все соединения."""
        await self._direct.aclose()
        for transport in self._proxies.values():
            await transport.aclose()
        self._proxies.clear()


def routed_client(service: str | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """`httpx.AsyncClient`, ходящий через маршруты сервиса (без `service` сервис определяется по хосту каждого запроса). Параметр `verify` передаётся транспорту."""
    verify = kwargs.pop('verify', True)
    kwargs.pop('trust_env', None)
    return httpx.AsyncClient(transport=SmartTransport(service, verify=verify), **kwargs)
