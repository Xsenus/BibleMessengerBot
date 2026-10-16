"""Панель: журнал действий, отчёты о доставке, шаблоны объявлений, предпросмотр, роли администраторов."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services import announcements, audit, fanout

PLATFORM_NAMES = ('telegram', 'max', 'vk')


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    title: str = Field('', max_length=200)
    body: str = Field(min_length=1, max_length=3500)


class PreviewIn(BaseModel):
    title: str = Field('', max_length=200)
    body: str = Field(min_length=1, max_length=3500)
    audience: dict = {}
    asset_id: int | None = None


class RoleIn(BaseModel):
    role: Literal['viewer', 'editor', 'admin']


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    router = APIRouter(prefix='/api/admin', dependencies=[Depends(auth)])

    async def connection():
        pool = await pool_factory()
        async with pool.acquire() as conn:
            yield conn

    Conn = Depends(connection)

    @router.get('/audit')
    async def audit_log(limit: int = 100, offset: int = 0, actor: str = '', action: str = '', source: str = '',
                        q: str = '', days: int = 0, conn: Any = Conn) -> dict:
        """Журнал действий администраторов (новые сверху), по страницам; has_more — есть записи старее."""
        limit = max(1, min(limit, 499))
        items = await audit.recent(conn, limit + 1, offset, actor, action, source, q, days)
        return dict(items=items[:limit], has_more=len(items) > limit)

    @router.get('/audit/actions')
    async def audit_actions(conn: Any = Conn) -> list[str]:
        """Какие действия встречаются в журнале — для фильтра."""
        return await audit.actions(conn)

    @router.post('/announcements/preview')
    async def preview(payload: PreviewIn, conn: Any = Conn) -> dict:
        """Как объявление выглядит на каждой платформе (части сообщения) и сколько получателей на ней."""
        text = announcements.render(payload.title, payload.body)
        result = {}
        for platform in PLATFORM_NAMES:
            audience = fanout.normalize_audience(dict(payload.audience, platforms=[platform]))
            if payload.audience.get('platforms') and platform not in payload.audience['platforms']:
                result[platform] = dict(recipients=0, parts=[])
                continue
            parts = fanout.chunks_for(platform, text, payload.asset_id)
            result[platform] = dict(recipients=await fanout.count_recipients(conn, audience),
                                    parts=[p if isinstance(p, str) else p.get('caption') or '[картинка]' if isinstance(p, dict) else str(p) for p in parts])
        return dict(platforms=result, total=sum(item['recipients'] for item in result.values()))

    # --- Шаблоны ------------------------------------------------------------------------------
    @router.get('/announcement-templates')
    async def list_templates(conn: Any = Conn) -> list[dict]:
        return [dict(r) | dict(created_at=r['created_at'].isoformat()) for r in await conn.fetch('SELECT * FROM announcement_templates ORDER BY name')]

    @router.post('/announcement-templates', status_code=201)
    async def save_template(payload: TemplateIn, conn: Any = Conn) -> dict:
        """Создать шаблон или заменить существующий с тем же названием."""
        identifier = await conn.fetchval(
            """INSERT INTO announcement_templates(name,title,body) VALUES($1,$2,$3)
               ON CONFLICT(name) DO UPDATE SET title=EXCLUDED.title,body=EXCLUDED.body RETURNING id""",
            payload.name.strip(), payload.title, payload.body)
        return dict(id=identifier)

    @router.delete('/announcement-templates/{template_id}')
    async def delete_template(template_id: int, conn: Any = Conn) -> dict:
        if await conn.execute('DELETE FROM announcement_templates WHERE id=$1', template_id) != 'DELETE 1':
            raise HTTPException(404, 'not_found')
        return dict(ok=True)

    # --- Роли ---------------------------------------------------------------------------------
    @router.put('/admins/{user_id}/role')
    async def set_role(user_id: int, payload: RoleIn, conn: Any = Conn) -> dict:
        """Изменить роль администратора (владельца изменить нельзя)."""
        done = await conn.execute(
            'UPDATE telegram_users SET admin_role=$2,updated_at=now() WHERE telegram_user_id=$1 AND is_admin AND NOT is_owner', user_id, payload.role)
        if done != 'UPDATE 1':
            raise HTTPException(404, 'not_found')
        return dict(ok=True)

    # --- Отчёты о доставке --------------------------------------------------------------------
    @router.get('/delivery-report')
    async def delivery_report(days: int = 7, conn: Any = Conn) -> dict:
        """Доставки по дням и видам, частые ошибки и итоги запусков объявлений за `days` суток."""
        days = max(1, min(days, 90))
        per_day = await conn.fetch(
            """SELECT (created_at AT TIME ZONE 'UTC')::date AS day,mode,
               count(*) AS total,count(*) FILTER (WHERE status='sent') AS sent,
               count(*) FILTER (WHERE status IN ('failed','uncertain')) AS failed
               FROM delivery_log WHERE created_at>now()-make_interval(days=>$1) GROUP BY 1,2 ORDER BY 1 DESC,2""", days)
        errors = await conn.fetch(
            """SELECT COALESCE(error_code,'—') AS error_code,mode,count(*) AS n FROM delivery_log
               WHERE status IN ('failed','uncertain','retry') AND created_at>now()-make_interval(days=>$1)
               GROUP BY 1,2 ORDER BY n DESC LIMIT 20""", days)
        runs = await conn.fetch(
            """SELECT r.id,r.announcement_id,a.title,r.created_at,r.status,r.recipients,r.queued,r.skipped,
               (SELECT count(*) FROM delivery_log d WHERE d.payload_key='announcement:'||r.id AND d.status='sent') AS sent,
               (SELECT count(*) FROM delivery_log d WHERE d.payload_key='announcement:'||r.id AND d.status IN ('failed','uncertain')) AS failed
               FROM announcement_runs r JOIN announcements a ON a.id=r.announcement_id
               WHERE r.created_at>now()-make_interval(days=>$1) ORDER BY r.id DESC LIMIT 50""", days)
        return dict(per_day=[dict(r) | dict(day=r['day'].isoformat()) for r in per_day], errors=[dict(r) for r in errors],
                    runs=[dict(r) | dict(created_at=r['created_at'].isoformat()) for r in runs])

    return router
