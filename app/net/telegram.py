"""Сессия aiogram с выбором маршрута: те же правила, что у `SmartTransport`, но для Telegram."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from app.net.router import DIRECT, ROUTER, Route, Router

try:
    import aiohttp
    CONNECT_PHASE: tuple[type[BaseException], ...] = (aiohttp.ClientConnectorError, aiohttp.ClientProxyConnectionError)
except ImportError:  # pragma: no cover
    CONNECT_PHASE = ()

# Методы, безопасные для повтора после любой сетевой ошибки (ничего не меняют на стороне Telegram).
READ_ONLY = {'getUpdates', 'getMe', 'getFile', 'getChat', 'getChatMember', 'getWebhookInfo', 'getMyCommands'}


def proxy_url(url: str | None) -> str | None:
    """aiogram uses remote DNS for SOCKS5; HTTPS proxy connectors are unsupported."""
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme == 'socks5h':
        return urlunsplit(parts._replace(scheme='socks5'))
    if parts.scheme not in {'http', 'socks5'}:
        raise ValueError('unsupported_telegram_proxy_scheme')
    return url


class RoutedSession(AiohttpSession):
    """Сессия Bot API: каждому маршруту соответствует собственная дочерняя сессия (со своим прокси)."""

    def __init__(self, service: str = 'telegram', *, router: Router = ROUTER, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.service, self.router = service, router
        self._children: dict[str, tuple[str | None, AiohttpSession]] = {}
        self._retired: list[AiohttpSession] = []

    def _child(self, route: Route) -> AiohttpSession:
        """Дочерняя сессия маршрута; пересоздаётся при смене адреса прокси."""
        cached = self._children.get(route.key)
        if cached is None or cached[0] != route.proxy_url:
            session = AiohttpSession(proxy=proxy_url(route.proxy_url), timeout=self.timeout)
            if cached is not None:
                self._retired.append(cached[1])
            self._children[route.key] = (route.proxy_url, session)
            return session
        return cached[1]

    async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
        """Выполнить метод Bot API по лучшему маршруту; при сбое соединения — по следующему."""
        last: Exception | None = None
        for route in self.router.plan(self.service) or [DIRECT]:
            started = time.monotonic()
            try:
                child = self._child(route)
            except (ValueError, RuntimeError, ImportError) as error:
                # Construction failed before any request: even a mutation can safely use another route.
                self.router.record_failure(self.service, route, type(error).__name__)
                last = error
                continue
            try:
                result = await child.make_request(bot, method, timeout)
            except TelegramNetworkError as error:
                self.router.record_failure(self.service, route, type(error.__cause__ or error).__name__)
                last = error
                if isinstance(error.__cause__, CONNECT_PHASE) or getattr(method, '__api_method__', '') in READ_ONLY:
                    continue
                raise
            self.router.record_success(self.service, route, (time.monotonic() - started) * 1000)
            return result
        assert last is not None
        raise last

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> Any:
        """Скачивание файла: маршрут меняется только до получения первого куска (после этого поток уже идёт)."""
        last: Exception | None = None
        for route in self.router.plan(self.service) or [DIRECT]:
            started, produced = time.monotonic(), False
            try:
                async for chunk in self._child(route).stream_content(url, headers, timeout, chunk_size, raise_for_status):
                    if not produced:
                        self.router.record_success(self.service, route, (time.monotonic() - started) * 1000)
                    produced = True
                    yield chunk
                return
            except Exception as error:                                   # noqa: BLE001 — любой сбой соединения
                if produced:
                    raise
                self.router.record_failure(self.service, route, type(error).__name__)
                last = error
        assert last is not None
        raise last

    async def close(self) -> None:
        """Закрыть все дочерние сессии."""
        for _, session in self._children.values():
            await session.close()
        self._children.clear()
        for session in self._retired:
            await session.close()
        self._retired.clear()
        await super().close()
