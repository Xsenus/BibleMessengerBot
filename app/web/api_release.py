"""Защищённое чтение версии приложения, квитанции деплоя и сигналов ботов без доступа к Docker socket."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Response

from app.services import release_info

github = release_info.GithubDeployments()


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    """Собрать защищённый маршрут; соединение БД освобождается до запроса GitHub."""
    router = APIRouter(prefix='/api/admin', dependencies=[Depends(auth)])

    @router.get('/release')
    async def release(response: Response) -> dict:
        """Вернуть версию из образа, историю установки и текущее состояние сигналов, не публикуя внутренние ошибки."""
        response.headers['Cache-Control'] = 'no-store'
        build = release_info.build_info()
        server = await asyncio.to_thread(release_info.server_info)
        beats = None
        try:
            async with asyncio.timeout(3):
                pool = await pool_factory()
                async with pool.acquire() as conn:
                    rows = await conn.fetch("""SELECT service,last_seen,
                        last_seen>now()-interval '180 seconds' AND last_seen<=now()+interval '10 seconds' AS fresh
                        FROM service_heartbeats WHERE service IN ('bot','maxbot','vkbot')""")
                beats = {row['service']: row for row in rows}
        except Exception:
            # Диагностика версии остаётся доступной при отказе БД; наружу исключение не выводится.
            pass
        current = server['current']
        verified = {item['name']: item for item in current['services']} if current else {}
        bots = []
        for name, label in [('bot', 'Telegram'), ('maxbot', 'MAX'), ('vkbot', 'ВКонтакте')]:
            beat = beats.get(name) if beats is not None else None
            bots.append({'service': name, 'label': label, 'expected': name in verified if current else None,
                         'verified': bool(build['commit'] and current and current['commit'] == build['commit']
                                          and name in verified and verified[name]['commit'] == build['commit']),
                         'live': bool(beat['fresh']) if beat is not None else False if beats is not None else None,
                         'last_seen': beat['last_seen'].isoformat() if beat is not None else None})
        return {'build': build, 'server': server, 'github': await github.get(), 'bots': bots}

    return router
