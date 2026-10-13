"""Хранение маршрутов в БД, загрузка в роутер и сохранение снимка здоровья."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any
from urllib.parse import quote, urlsplit

from app.net.router import DIRECT, ROUTER, Policy, Route
from app.services import secrets_box

LOGGER = logging.getLogger(__name__)
SCHEMES = ('http', 'https', 'socks5', 'socks5h')
PROCESS = f'{os.getenv("HOSTNAME", socket.gethostname())}:{os.getpid()}'


def parse_proxy_url(url: str) -> tuple[str, str]:
    """Проверить адрес прокси и вернуть (схема, хост:порт для показа). Логин и пароль допустимы в адресе."""
    parts = urlsplit(url.strip())
    if parts.scheme not in SCHEMES or not parts.hostname or parts.path not in {'', '/'} or parts.query or parts.fragment:
        raise ValueError('proxy_url')
    try:
        port = parts.port
    except ValueError:
        raise ValueError('proxy_url') from None
    if port is None or not 1 <= port <= 65535:
        raise ValueError('proxy_port')
    return parts.scheme, f'{parts.hostname}:{port}'


def build_url(scheme: str, host: str, port: int, user: str = '', password: str = '') -> str:
    """Собрать адрес прокси из частей (логин и пароль экранируются)."""
    auth = f'{quote(user, safe="")}:{quote(password, safe="")}@' if user else ''
    return f'{scheme}://{auth}{host}:{port}'


async def load(connection: Any) -> dict[str, Policy]:
    """Прочитать политики сервисов из БД (расшифровав адреса прокси; нечитаемые прокси пропускаются)."""
    proxies = {}
    for row in await connection.fetch('SELECT id,name,url_enc FROM net_proxies WHERE is_enabled'):
        try:
            proxies[row['id']] = Route(f"proxy:{row['id']}", row['name'], secrets_box.decrypt(row['url_enc']))
        except (ValueError, RuntimeError):
            LOGGER.error('Proxy %s skipped: secret cannot be decrypted', row['id'])
    policies = {}
    for policy in await connection.fetch('SELECT * FROM net_policies'):
        routes: list[Route] = []
        for item in await connection.fetch('SELECT proxy_id FROM net_routes WHERE service=$1 AND is_enabled ORDER BY position,id', policy['service']):
            route = DIRECT if item['proxy_id'] is None else proxies.get(item['proxy_id'])
            if route is not None:
                routes.append(route)
        policies[policy['service']] = Policy(policy['strategy'], policy['attempts'], routes or [DIRECT])
    return policies


async def refresh(connection: Any) -> None:
    """Обновить настройки роутера из БД."""
    ROUTER.set_policies(await load(connection))


async def flush_health(connection: Any) -> None:
    """Сохранить снимок здоровья маршрутов этого процесса для панели."""
    for item in ROUTER.snapshot():
        await connection.execute('''INSERT INTO net_health(service,route,process,successes,failures,consecutive,latency_ms,open_seconds,last_error)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(service,route,process) DO UPDATE SET successes=EXCLUDED.successes,
            failures=EXCLUDED.failures,consecutive=EXCLUDED.consecutive,latency_ms=EXCLUDED.latency_ms,
            open_seconds=EXCLUDED.open_seconds,last_error=EXCLUDED.last_error,updated_at=now()''',
                                 item['service'], item['route'], PROCESS, item['successes'], item['failures'], item['consecutive'],
                                 item['latency_ms'], item['open_seconds'], item['last_error'])


async def refresh_loop(pool: Any, interval: float = 30) -> None:
    """Фоновое обновление настроек и сохранение здоровья; сбой БД не останавливает службу (остаются прежние настройки)."""
    while True:
        try:
            async with pool.acquire() as connection:
                await refresh(connection)
                await flush_health(connection)
                await connection.execute("DELETE FROM net_health WHERE updated_at<now()-interval '1 day'")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning('Network routes refresh deferred (%s)', type(error).__name__)
        await asyncio.sleep(interval)
