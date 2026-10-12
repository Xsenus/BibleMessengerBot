"""Программный интерфейс веб-панели администратора (`/api/admin/*`): объявления, источники, видеоканалы, администраторы.

Доступ — по ключу `X-Admin-Key` (см. `require_key`). Вся бизнес-логика находится в сервисах, общих с командами бота,
поэтому панель и бот ведут себя одинаково. Ошибки пользователя превращаются в понятные коды 4xx, внутренние — в 500
без подробностей.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import accounts, announcements, chat_sources, fanout, video_watch

MAX_IMAGE_REQUEST = 6 * 1024 * 1024


class AudienceIn(BaseModel):
    """Аудитория рассылки: пустые поля означают «все»."""
    platforms: list[Literal['telegram', 'max', 'vk']] = []
    chat_types: list[Literal['private', 'group', 'channel']] = []
    chat_ids: list[int] = Field(default=[], max_length=500)


class AnnouncementIn(BaseModel):
    """Новое объявление с расписанием."""
    title: str = Field('', max_length=200)
    body: str = Field(min_length=1, max_length=3500)
    kind: Literal['once', 'daily', 'weekly', 'monthly', 'interval'] = 'once'
    send_time: time | None = None
    timezone: str = 'UTC'
    days_of_week: list[int] = [1, 2, 3, 4, 5, 6, 7]
    day_of_month: int | None = Field(None, ge=1, le=31)
    interval_minutes: int | None = Field(None, ge=5)
    start_at: datetime | None = None
    end_at: datetime | None = None
    max_runs: int | None = Field(None, ge=1)
    audience: AudienceIn = AudienceIn()
    asset_id: int | None = None


class SourcePatch(BaseModel):
    """Изменяемые поля источника."""
    is_enabled: bool | None = None
    mode: Literal['capture', 'approve', 'auto'] | None = None
    prefix: str | None = Field(None, max_length=200)
    min_length: int | None = Field(None, ge=1, le=3000)
    audience: AudienceIn | None = None


class FeedIn(BaseModel):
    """Новый видеоканал."""
    platform: Literal['youtube', 'rutube', 'vk']
    channel: str = Field(min_length=1, max_length=300)
    notify: bool = False


class FeedPatch(BaseModel):
    """Изменяемые поля видеоканала."""
    is_enabled: bool | None = None
    notify: bool | None = None
    title: str | None = Field(None, max_length=200)
    template: str | None = Field(None, max_length=500)
    interval_minutes: int | None = Field(None, ge=5, le=1440)
    audience: AudienceIn | None = None


class AnnouncementPatch(BaseModel):
    """Изменяемые поля объявления (расписание не меняется: создайте новое объявление)."""
    title: str | None = Field(None, max_length=200)
    body: str | None = Field(None, min_length=1, max_length=3500)
    audience: AudienceIn | None = None
    asset_id: int | None = None
    clear_asset: bool = False


class StatusIn(BaseModel):
    """Смена состояния объявления."""
    status: Literal['active', 'paused', 'cancelled']


def rows(items: Any) -> list[dict]:
    """Строки БД → словари, пригодные для JSON (даты в ISO, JSON-текст разворачивается)."""
    out = []
    for item in items:
        row = dict(item)
        for key, value in row.items():
            if isinstance(value, (datetime, time)):
                row[key] = value.isoformat()
            elif isinstance(value, str) and key in {'audience', 'meta'}:
                row[key] = json.loads(value)
        out.append(row)
    return out


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    """Собрать маршруты. `auth` — зависимость проверки ключа, `pool_factory` — получение пула соединений."""
    router = APIRouter(prefix='/api/admin', dependencies=[Depends(auth)])

    async def connection():
        """Соединение с БД на время запроса (ошибки пользователя превращаются в 4xx обработчиком приложения)."""
        pool = await pool_factory()
        async with pool.acquire() as conn:
            yield conn

    Conn = Depends(connection)

    async def owner_id(conn: Any) -> int:
        """Внутренний ID владельца: действия панели выполняются от его имени."""
        found = await conn.fetchval('SELECT telegram_user_id FROM telegram_users WHERE is_owner AND NOT is_blocked ORDER BY created_at LIMIT 1')
        if found is None:
            raise HTTPException(409, 'owner_not_set')
        return found

    @router.get('/overview')
    async def overview(conn: Any = Conn) -> dict:
        """Сводка: активные назначения по платформам, очередь доставки, ошибки, сигналы служб."""
        platforms = await conn.fetch('SELECT platform,count(*) AS count FROM telegram_chats WHERE is_active GROUP BY platform')
        queue = await conn.fetchrow('''SELECT
            count(*) FILTER (WHERE status IN ('pending','retry','sending')) AS queued,
            count(*) FILTER (WHERE status IN ('failed','uncertain')) AS attention,
            count(*) FILTER (WHERE status='sent' AND sent_at>now()-interval '1 day') AS sent_24h FROM delivery_log''')
        beats = await conn.fetch('SELECT service,last_seen FROM service_heartbeats ORDER BY service')
        return {'platforms': rows(platforms), 'queue': dict(queue), 'services': rows(beats)}

    # --- Объявления ---------------------------------------------------------------------------
    @router.get('/announcements')
    async def list_announcements(conn: Any = Conn) -> list[dict]:
        """Последние объявления с расписанием и ближайшим запуском."""
        return rows(await conn.fetch('SELECT * FROM announcements ORDER BY id DESC LIMIT 200'))

    @router.post('/announcements', status_code=201)
    async def create_announcement(payload: AnnouncementIn, conn: Any = Conn) -> dict:
        """Создать объявление (разовое или по расписанию)."""
        schedule = payload.model_dump(include={'kind', 'send_time', 'timezone', 'days_of_week', 'day_of_month',
                                               'interval_minutes', 'start_at', 'end_at', 'max_runs'})
        if payload.kind in {'daily', 'weekly', 'monthly'} and schedule['send_time'] is None:
            raise HTTPException(422, 'send_time_required')
        if payload.kind == 'monthly' and schedule['day_of_month'] is None:
            raise HTTPException(422, 'day_of_month_required')
        if payload.kind == 'interval' and schedule['interval_minutes'] is None:
            raise HTTPException(422, 'interval_required')
        if not set(schedule['days_of_week']) <= set(range(1, 8)) or not schedule['days_of_week']:
            raise HTTPException(422, 'invalid_days')
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(schedule['timezone'])
        except Exception:
            raise HTTPException(422, 'invalid_timezone') from None
        row = await announcements.create(conn, await owner_id(conn), payload.title, payload.body, schedule,
                                         audience=payload.audience.model_dump(), asset_id=payload.asset_id)
        return rows([row])[0]

    @router.patch('/announcements/{announcement_id}')
    async def patch_announcement(announcement_id: int, payload: AnnouncementPatch, conn: Any = Conn) -> dict:
        """Изменить текст, заголовок, аудиторию или картинку объявления; уже созданные запуски не меняются."""
        values = payload.model_dump(exclude_none=True, exclude={'clear_asset'})
        if 'audience' in values:
            values['audience'] = json.dumps(fanout.normalize_audience(values['audience']))
        if payload.clear_asset:
            values['asset_id'] = None
        if not values:
            raise HTTPException(422, 'nothing_to_change')
        names = list(values)                                                  # имена столбцов приходят только из модели выше
        sets = ','.join(f'{n}=${i + 2}' + ('::jsonb' if n == 'audience' else '') for i, n in enumerate(names))
        done = await conn.execute(f"UPDATE announcements SET {sets},updated_at=now() WHERE id=$1 AND status IN ('active','paused')",  # noqa: S608
                                  announcement_id, *values.values())
        if done != 'UPDATE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.post('/announcements/{announcement_id}/status')
    async def announcement_status(announcement_id: int, payload: StatusIn, conn: Any = Conn) -> dict:
        """Приостановить, возобновить или отменить объявление."""
        if not await announcements.set_status(conn, announcement_id, payload.status):
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.post('/announcements/{announcement_id}/run')
    async def announcement_run(announcement_id: int, conn: Any = Conn) -> dict:
        """Отправить объявление прямо сейчас, не меняя расписание."""
        if not await announcements.run_now(conn, announcement_id):
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.post('/media', status_code=201)
    async def upload_media(request: Request, conn: Any = Conn) -> dict:
        """Загрузить картинку (тело запроса — файл PNG/JPEG/WebP) и получить `asset_id` для объявления."""
        data = await request.body()
        if len(data) > MAX_IMAGE_REQUEST:
            raise HTTPException(413, 'too_large')
        asset = await chat_sources.store_asset(conn, data, origin='panel')
        if asset is None:
            raise HTTPException(415, 'unsupported_image')
        return {'asset_id': asset}

    @router.get('/audience')
    async def audience_size(platform: str = '', chat_type: str = '', conn: Any = Conn) -> dict:
        """Сколько назначений получит рассылка с такой аудиторией."""
        audience = {'platforms': [platform] if platform else [], 'chat_types': [chat_type] if chat_type else []}
        return {'recipients': len(await fanout.recipients(conn, audience))}

    # --- Источники контента -------------------------------------------------------------------
    @router.get('/sources')
    async def list_sources(conn: Any = Conn) -> list[dict]:
        """Источники: каналы, группы и сообщества, где состоит бот."""
        return rows(await conn.fetch('SELECT * FROM content_sources ORDER BY is_enabled DESC,id DESC LIMIT 500'))

    @router.patch('/sources/{source_id}')
    async def patch_source(source_id: int, payload: SourcePatch, conn: Any = Conn) -> dict:
        """Включить или выключить источник, сменить режим, приставку и аудиторию копий."""
        values = payload.model_dump(exclude_none=True)
        if 'audience' in values:
            values['audience'] = json.dumps(fanout.normalize_audience(values['audience']))
        if not values:
            raise HTTPException(422, 'nothing_to_change')
        names = list(values)                                       # имена столбцов приходят только из модели выше
        sets = ','.join(f'{name}=${i + 2}' + ('::jsonb' if name == 'audience' else '') for i, name in enumerate(names))
        done = await conn.execute(f'UPDATE content_sources SET {sets} WHERE id=$1', source_id, *values.values())  # noqa: S608
        if done != 'UPDATE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.get('/posts')
    async def list_posts(status: Literal['new', 'published', 'ignored'] = 'new', conn: Any = Conn) -> list[dict]:
        """Собранные посты источников."""
        return rows(await conn.fetch('''SELECT p.id,p.source_id,p.body,p.status,p.posted_at,s.title AS source_title,s.platform
            FROM source_posts p JOIN content_sources s ON s.id=p.source_id WHERE p.status=$1 ORDER BY p.id DESC LIMIT 100''', status))

    @router.post('/posts/{post_id}/publish')
    async def publish_post(post_id: int, conn: Any = Conn) -> dict:
        """Отправить копию поста пользователям."""
        return await chat_sources.publish(conn, post_id)

    @router.post('/posts/{post_id}/ignore')
    async def ignore_post(post_id: int, conn: Any = Conn) -> dict:
        """Пропустить пост."""
        if not await chat_sources.ignore(conn, post_id):
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    # --- Видеоканалы --------------------------------------------------------------------------
    @router.get('/videos')
    async def list_feeds(conn: Any = Conn) -> list[dict]:
        """Отслеживаемые видеоканалы."""
        return rows(await conn.fetch('SELECT * FROM video_feeds ORDER BY id DESC LIMIT 200'))

    @router.post('/videos', status_code=201)
    async def add_feed(payload: FeedIn, conn: Any = Conn) -> dict:
        """Добавить канал (ID или ссылка)."""
        return rows([await video_watch.add(conn, payload.platform, payload.channel, notify=payload.notify)])[0]

    @router.patch('/videos/{feed_id}')
    async def patch_feed(feed_id: int, payload: FeedPatch, conn: Any = Conn) -> dict:
        """Изменить настройки канала: опрос, рассылка, название, шаблон, интервал, аудитория."""
        values = payload.model_dump(exclude_none=True)
        if 'audience' in values:
            values['audience'] = json.dumps(fanout.normalize_audience(values['audience']))
        if not values:
            raise HTTPException(422, 'nothing_to_change')
        names = list(values)
        sets = ','.join(f'{name}=${i + 2}' + ('::jsonb' if name == 'audience' else '') for i, name in enumerate(names))
        done = await conn.execute(f'UPDATE video_feeds SET {sets},next_check_at=LEAST(next_check_at,now()) WHERE id=$1',  # noqa: S608
                                  feed_id, *values.values())
        if done != 'UPDATE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.delete('/videos/{feed_id}')
    async def delete_feed(feed_id: int, conn: Any = Conn) -> dict:
        """Удалить канал вместе с историей его видео."""
        if await conn.execute('DELETE FROM video_feeds WHERE id=$1', feed_id) != 'DELETE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    # --- Администраторы -----------------------------------------------------------------------
    @router.get('/admins')
    async def list_admins(conn: Any = Conn) -> list[dict]:
        """Владелец и администраторы бота на всех платформах."""
        return rows(await accounts.list_admins(conn))

    @router.post('/admins/invite', status_code=201)
    async def invite_admin(conn: Any = Conn) -> dict:
        """Создать одноразовый код приглашения; новый администратор отправляет боту `/claim КОД`."""
        return {'code': await accounts.create_admin_invite(conn, await owner_id(conn))}

    @router.delete('/admins/{user_id}')
    async def remove_admin(user_id: int, conn: Any = Conn) -> dict:
        """Снять права администратора."""
        if not await accounts.remove_admin(conn, await owner_id(conn), user_id):
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    return router
