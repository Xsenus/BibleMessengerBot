"""Панель администратора: сетевые маршруты (прокси, порядок и стратегия для каждого внешнего сервиса)."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.net import store
from app.net.router import STRATEGIES
from app.net.services import SERVICES
from app.services import secrets_box


class ProxyIn(BaseModel):
    """Новый прокси: либо готовый адрес `url`, либо части."""
    name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$')
    url: str = Field('', max_length=400)
    scheme: Literal['http', 'https', 'socks5', 'socks5h'] = 'http'
    host: str = Field('', max_length=200)
    port: int = Field(0, ge=0, le=65535)
    user: str = Field('', max_length=200)
    password: str = Field('', max_length=200)


class ProxyPatch(BaseModel):
    """Изменяемые поля прокси; адрес заменяется целиком только если передан `url` или `host`."""
    name: str | None = Field(None, pattern=r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$')
    url: str | None = Field(None, max_length=400)
    is_enabled: bool | None = None


class RouteIn(BaseModel):
    """Маршрут сервиса: `proxy_id` пуст — прямой доступ."""
    proxy_id: int | None = None
    enabled: bool = True


class PolicyIn(BaseModel):
    """Политика сервиса: стратегия, число попыток на запрос и упорядоченный список маршрутов."""
    strategy: Literal['failover', 'auto', 'round_robin'] = 'failover'
    attempts: int = Field(3, ge=1, le=10)
    routes: list[RouteIn] = Field(default=[], max_length=20)


class TestIn(BaseModel):
    """Какой сервис использовать для проверки прокси."""
    service: str = 'telegram'


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    """Собрать маршруты `/api/admin/net/*`."""
    router = APIRouter(prefix='/api/admin/net', dependencies=[Depends(auth)])

    async def connection():
        """Соединение с БД на время запроса."""
        pool = await pool_factory()
        async with pool.acquire() as conn:
            yield conn

    Conn = Depends(connection)

    def need_secrets() -> None:
        """Адреса прокси (с логином и паролем) хранятся зашифрованными."""
        try:
            secrets_box.encrypt('probe')
        except RuntimeError:
            raise HTTPException(409, 'secrets_key_missing') from None

    def proxy_url(payload: ProxyIn) -> str:
        """Адрес прокси из `url` или из частей; неверный формат — 422."""
        url = payload.url or (store.build_url(payload.scheme, payload.host, payload.port, payload.user, payload.password) if payload.host else '')
        try:
            store.parse_proxy_url(url)
        except ValueError as error:
            raise HTTPException(422, 'invalid_' + str(error)) from None
        return url

    async def probe(url: str | None, service: str) -> dict:
        """Проверка доступности сервиса по маршруту: любой HTTP-ответ означает, что путь работает."""
        target = SERVICES[service][1]
        started = time.monotonic()
        try:
            transport = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(url)) if url else httpx.AsyncHTTPTransport()
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10, connect=8), follow_redirects=False) as client:
                response = await client.get(target)
            return dict(ok=True, status=response.status_code, ms=round((time.monotonic() - started) * 1000), error='')
        except httpx.HTTPError as error:
            return dict(ok=False, status=0, ms=round((time.monotonic() - started) * 1000), error=type(error).__name__)
        except Exception as error:                               # например, нет пакета для SOCKS
            return dict(ok=False, status=0, ms=0, error=type(error).__name__)

    def public(row: Any) -> dict:
        """Прокси без адреса (в нём может быть пароль)."""
        data = {k: row[k] for k in ('id', 'name', 'scheme', 'host', 'is_enabled', 'last_test_ok', 'last_test_error')}
        data['last_test_at'] = row['last_test_at'].isoformat() if row['last_test_at'] else None
        return data

    @router.get('')
    async def overview(conn: Any = Conn) -> dict:
        """Сервисы с их маршрутами и здоровьем, список прокси и стратегии."""
        proxies = {r['id']: r for r in await conn.fetch('SELECT * FROM net_proxies ORDER BY id')}
        policies = {r['service']: r for r in await conn.fetch('SELECT * FROM net_policies')}
        routes: dict[str, list] = {}
        for r in await conn.fetch('SELECT * FROM net_routes ORDER BY position,id'):
            routes.setdefault(r['service'], []).append(r)
        health: dict[tuple[str, str], dict] = {}
        for h in await conn.fetch("SELECT * FROM net_health WHERE updated_at>now()-interval '10 minutes'"):
            item = health.setdefault((h['service'], h['route']), dict(successes=0, failures=0, latency_ms=None, open_seconds=0, last_error=''))
            item['successes'] += h['successes']
            item['failures'] += h['failures']
            item['open_seconds'] = max(item['open_seconds'], h['open_seconds'])
            item['latency_ms'] = h['latency_ms'] if item['latency_ms'] is None else min(item['latency_ms'], h['latency_ms'] or item['latency_ms'])
            item['last_error'] = item['last_error'] or h['last_error']
        services = []
        for key, (label, target) in SERVICES.items():
            policy = policies.get(key)
            items = []
            for r in routes.get(key, []):
                proxy = proxies.get(r['proxy_id']) if r['proxy_id'] is not None else None
                route_key = 'direct' if r['proxy_id'] is None else f"proxy:{r['proxy_id']}"
                items.append(dict(proxy_id=r['proxy_id'], name='Напрямую' if proxy is None and r['proxy_id'] is None else (proxy['name'] if proxy else '?'),
                                  enabled=r['is_enabled'], health=health.get((key, route_key))))
            services.append(dict(key=key, label=label, target=target, strategy=policy['strategy'] if policy else 'failover',
                                 attempts=policy['attempts'] if policy else 3, configured=policy is not None,
                                 routes=items or [dict(proxy_id=None, name='Напрямую', enabled=True, health=health.get((key, 'direct')))]))
        return dict(services=services, proxies=[public(p) for p in proxies.values()], strategies=list(STRATEGIES), secrets_ready=_ready())

    def _ready() -> bool:
        """Есть ли ключ шифрования для хранения адресов прокси."""
        try:
            secrets_box.encrypt('probe')
            return True
        except RuntimeError:
            return False

    @router.post('/proxies', status_code=201)
    async def add_proxy(payload: ProxyIn, conn: Any = Conn) -> dict:
        """Добавить прокси (адрес хранится зашифрованным)."""
        need_secrets()
        url = proxy_url(payload)
        scheme, host = store.parse_proxy_url(url)
        if await conn.fetchval('SELECT 1 FROM net_proxies WHERE lower(name)=lower($1)', payload.name):
            raise HTTPException(409, 'name_taken')
        row = await conn.fetchrow('INSERT INTO net_proxies(name,scheme,host,url_enc) VALUES($1,$2,$3,$4) RETURNING *',
                                  payload.name, scheme, host, secrets_box.encrypt(url))
        return public(row)

    @router.patch('/proxies/{proxy_id}')
    async def patch_proxy(proxy_id: int, payload: ProxyPatch, conn: Any = Conn) -> dict:
        """Изменить прокси: название, адрес, включение."""
        row = await conn.fetchrow('SELECT * FROM net_proxies WHERE id=$1', proxy_id)
        if not row:
            raise HTTPException(404, 'not_found')
        values: dict[str, Any] = {}
        if payload.name is not None:
            values['name'] = payload.name
        if payload.is_enabled is not None:
            values['is_enabled'] = payload.is_enabled
        if payload.url:
            need_secrets()
            try:
                scheme, host = store.parse_proxy_url(payload.url)
            except ValueError as error:
                raise HTTPException(422, 'invalid_' + str(error)) from None
            values.update(scheme=scheme, host=host, url_enc=secrets_box.encrypt(payload.url))
        if not values:
            raise HTTPException(422, 'nothing_to_change')
        names = list(values)                                                 # имена столбцов берутся только из модели выше
        sets = ','.join(f'{n}=${i + 2}' for i, n in enumerate(names))
        updated = await conn.fetchrow(f'UPDATE net_proxies SET {sets} WHERE id=$1 RETURNING *', proxy_id, *values.values())  # noqa: S608
        return public(updated)

    @router.delete('/proxies/{proxy_id}')
    async def delete_proxy(proxy_id: int, conn: Any = Conn) -> dict:
        """Удалить прокси вместе с его местом в маршрутах сервисов."""
        if await conn.execute('DELETE FROM net_proxies WHERE id=$1', proxy_id) != 'DELETE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.post('/proxies/{proxy_id}/test')
    async def test_proxy(proxy_id: int, payload: TestIn, conn: Any = Conn) -> dict:
        """Проверить прокси: запрос к адресу выбранного сервиса через него."""
        if payload.service not in SERVICES:
            raise HTTPException(422, 'invalid_service')
        row = await conn.fetchrow('SELECT * FROM net_proxies WHERE id=$1', proxy_id)
        if not row:
            raise HTTPException(404, 'not_found')
        try:
            url = secrets_box.decrypt(row['url_enc'])
        except (ValueError, RuntimeError):
            raise HTTPException(409, 'secret_unreadable') from None
        result = await probe(url, payload.service)
        await conn.execute('UPDATE net_proxies SET last_test_at=now(),last_test_ok=$2,last_test_error=$3 WHERE id=$1',
                           proxy_id, result['ok'], result['error'])
        return result

    @router.put('/services/{service}')
    async def set_policy(service: str, payload: PolicyIn, conn: Any = Conn) -> dict:
        """Задать стратегию и упорядоченные маршруты сервиса (пустой список — только прямой доступ)."""
        if service not in SERVICES:
            raise HTTPException(404, 'not_found')
        ids = [r.proxy_id for r in payload.routes]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, 'duplicate_route')
        real = [i for i in ids if i is not None]
        if real and await conn.fetchval('SELECT count(*) FROM net_proxies WHERE id=ANY($1::bigint[])', real) != len(real):
            raise HTTPException(422, 'invalid_proxy')
        async with conn.transaction():
            await conn.execute('''INSERT INTO net_policies(service,strategy,attempts) VALUES($1,$2,$3)
                ON CONFLICT(service) DO UPDATE SET strategy=EXCLUDED.strategy,attempts=EXCLUDED.attempts,updated_at=now()''',
                               service, payload.strategy, payload.attempts)
            await conn.execute('DELETE FROM net_routes WHERE service=$1', service)
            for position, route in enumerate(payload.routes):
                await conn.execute('INSERT INTO net_routes(service,proxy_id,position,is_enabled) VALUES($1,$2,$3,$4)',
                                   service, route.proxy_id, position, route.enabled)
        await store.refresh(conn)
        return {'ok': True}

    @router.post('/services/{service}/probe')
    async def probe_service(service: str, conn: Any = Conn) -> list[dict]:
        """Проверить все включённые маршруты сервиса сейчас и показать, какой быстрее."""
        if service not in SERVICES:
            raise HTTPException(404, 'not_found')
        policies = await store.load(conn)
        policy = policies.get(service)
        routes = policy.routes if policy else []
        results = []
        for route in routes or [store.DIRECT]:
            results.append(dict(route=route.key, label=route.label, **await probe(route.proxy_url, service)))
        return results

    return router
