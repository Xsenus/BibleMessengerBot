"""Панель администратора: генераторы картинок, настройки разделов, чаты, пользователи и очередь доставки."""
from __future__ import annotations

import base64
import os
import time
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from PIL import Image
from pydantic import BaseModel, Field

from app.services import artwork, features, illustrations, free_image_providers, image_router, secrets_box
from app.services.image_providers import MODELS, GenerationError
from app.worker.delivery import resolve_uncertain

TEST_PROMPT = 'A quiet shepherd with sheep on a green hill at sunrise, soft watercolor, warm light, no text'


class ProviderIn(BaseModel):
    """Новый провайдер картинок."""
    slug: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{1,29}$')
    label: str = Field('', max_length=100)
    adapter: Literal['cloudflare', 'huggingface', 'pollinations', 'horde', 'openai_compatible']
    base_url: str = Field('', max_length=300)
    model: str = Field('', max_length=200)
    api_key: str = Field('', max_length=500)
    account_id: str = Field('', max_length=100)
    width: int = Field(1216, ge=256, le=2048)
    height: int = Field(832, ge=256, le=2048)
    steps: int | None = Field(None, ge=1, le=100)
    priority: int = Field(500, ge=0, le=100000)
    daily_limit: int = Field(20, ge=1, le=10000)
    is_enabled: bool = True
    extra: dict = {}


class ProviderPatch(BaseModel):
    """Изменяемые поля провайдера; `api_key` задаётся только для замены, `clear_key` удаляет ключ."""
    label: str | None = Field(None, max_length=100)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=200)
    api_key: str | None = Field(None, max_length=500)
    clear_key: bool = False
    account_id: str | None = Field(None, max_length=100)
    width: int | None = Field(None, ge=256, le=2048)
    height: int | None = Field(None, ge=256, le=2048)
    steps: int | None = Field(None, ge=1, le=100)
    priority: int | None = Field(None, ge=0, le=100000)
    daily_limit: int | None = Field(None, ge=1, le=10000)
    is_enabled: bool | None = None
    extra: dict | None = None


class SettingsIn(BaseModel):
    """Настройки разделов."""
    support_section: bool | None = None


class FlagIn(BaseModel):
    """Простое булево поле."""
    value: bool


class ResolveIn(BaseModel):
    """Решение по доставке с неопределённым исходом."""
    action: Literal['sent', 'retry-duplicate-risk', 'cancel']


def public(row: Any) -> dict:
    """Провайдер для панели: без ключа (только признак `has_key`)."""
    data = {k: v for k, v in dict(row).items() if k != 'api_key_enc'}
    data['has_key'] = bool(row['api_key_enc'])
    data['needs_key'] = free_image_providers.NEEDS_KEY.get(row['adapter'], True)
    for key in ('last_test_at', 'created_at', 'updated_at'):
        data[key] = data[key].isoformat() if data.get(key) else None
    return data


def build_router(auth: Callable[..., None], pool_factory: Callable[[], Awaitable[Any]]) -> APIRouter:
    """Собрать маршруты `/api/admin` для генераторов, настроек, чатов, пользователей и доставок."""
    router = APIRouter(prefix='/api/admin', dependencies=[Depends(auth)])

    async def connection():
        """Соединение с БД на время запроса."""
        pool = await pool_factory()
        async with pool.acquire() as conn:
            yield conn

    Conn = Depends(connection)

    async def owner_id(conn: Any) -> int:
        """Внутренний ID владельца: от его имени выполняются записи в журнал оператора."""
        found = await conn.fetchval('SELECT telegram_user_id FROM telegram_users WHERE is_owner AND NOT is_blocked ORDER BY created_at LIMIT 1')
        if found is None:
            raise HTTPException(409, 'owner_not_set')
        return found

    def need_secrets() -> None:
        """Ключи хранятся зашифрованными: без SECRETS_KEY/ADMIN_API_KEY сохранить их нельзя."""
        try:
            secrets_box.encrypt('probe')
        except RuntimeError:
            raise HTTPException(409, 'secrets_key_missing') from None

    # --- Генераторы картинок ----------------------------------------------------------------------
    @router.get('/image-providers')
    async def image_providers(conn: Any = Conn) -> dict:
        """Встроенные (платные) и добавленные генераторы, их использование за сутки, очередь заданий и шаблоны для добавления."""
        try:
            art = artwork.ArtSettings.from_env()
        except ValueError:
            raise HTTPException(500, 'invalid_image_settings') from None
        usage = {r['provider']: r['daily'] for r in await conn.fetch(
            """SELECT provider,count(*) AS daily FROM image_generation_attempts
               WHERE created_at >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') GROUP BY provider""")}
        health = {r['provider']: r for r in await conn.fetch(
            'SELECT provider,max(blocked_until) AS blocked_until,max(error_code) AS error_code FROM image_provider_health GROUP BY provider')}
        keys = {'openai': art.key, **art.extra_keys}
        builtin = []
        for name in art.provider_order:
            state = health.get(name)
            builtin.append(dict(name=name, model=MODELS[name], configured=bool(keys.get(name)), daily_used=usage.get(name, 0),
                                daily_limit=art.daily_requests,
                                blocked_until=state['blocked_until'].isoformat() if state and state['blocked_until'] else None,
                                error_code=state['error_code'] if state else None))
        custom = []
        for row in await conn.fetch('SELECT * FROM image_custom_providers ORDER BY priority,id'):
            item = public(row)
            item['daily_used'] = usage.get('x-' + row['slug'], 0)
            item['host'] = free_image_providers.host_of(dict(row))
            custom.append(item)
        jobs = {r['state']: r['n'] for r in await conn.fetch('SELECT state,count(*) AS n FROM image_generation_jobs GROUP BY state')}
        return dict(mode=art.provider, monthly_budget_usd=str(art.monthly_usd), builtin=builtin, custom=custom, jobs=jobs,
                    templates=free_image_providers.TEMPLATES, secrets_ready=_secrets_ready(), secrets_dedicated=bool(os.getenv('SECRETS_KEY', '').strip()),
                    planner_ready=await _planner_ready())

    class ImageTuning(BaseModel):
        ranking: str | None = Field(None, pattern='^(priority|auto)$')
        style: str | None = Field(None, max_length=300)

    @router.get('/image-tuning')
    async def image_tuning(conn: Any = Conn) -> dict:
        """Режим выбора генератора, общий стиль картинок и статистика генераторов за 7 суток."""
        values = {r['key']: r['value'] for r in await conn.fetch("SELECT key,value FROM app_settings WHERE key IN ('images.ranking','images.style')")}
        return dict(ranking=values.get('images.ranking', 'priority'), style=values.get('images.style', ''),
                    stats=await image_router.provider_stats(conn))

    @router.put('/image-tuning')
    async def set_image_tuning(payload: ImageTuning, conn: Any = Conn) -> dict:
        for key, value in (('images.ranking', payload.ranking), ('images.style', payload.style)):
            if value is not None:
                await conn.execute("""INSERT INTO app_settings(key,value) VALUES($1,$2)
                                      ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""", key, value.strip())
        image_router.RANKING['loaded_at'] = 0.0
        return await image_tuning(conn)

    @router.get('/illustrations')
    async def illustration_list(q: str = '', limit: int = 30, offset: int = 0, conn: Any = Conn) -> dict:
        """Готовые картинки с признаками закрепления/карантина; `q` — книга (код) для фильтра."""
        limit = max(1, min(limit, 100))
        rows = await conn.fetch(
            """SELECT id,book_code,chapter,verse,verse_end,artwork_scope,pinned,quarantined_at,generated_at,translation_id
               FROM verse_illustrations WHERE status='ready' AND ($1='' OR book_code=$1)
               ORDER BY generated_at DESC,id DESC LIMIT $2 OFFSET $3""", q.strip(), limit, max(0, offset))
        return dict(items=[dict(r, quarantined_at=r['quarantined_at'].isoformat() if r['quarantined_at'] else None,
                                generated_at=r['generated_at'].isoformat() if r['generated_at'] else None) for r in rows])

    @router.get('/illustrations/{image_id}/thumb')
    async def illustration_thumb(image_id: int, conn: Any = Conn) -> Response:
        row = await conn.fetchrow("SELECT image_data,mime_type FROM verse_illustrations WHERE id=$1 AND status='ready'", image_id)
        if not row:
            raise HTTPException(404, 'not_found')
        return Response(bytes(row['image_data']), media_type=row['mime_type'], headers={'Cache-Control': 'private, max-age=3600'})

    @router.post('/illustrations/{image_id}/pin')
    async def illustration_pin(image_id: int, pinned: bool = True, conn: Any = Conn) -> dict:
        if not await illustrations.set_pinned(conn, image_id, pinned):
            raise HTTPException(404, 'not_found')
        return dict(ok=True, pinned=pinned)

    @router.post('/illustrations/{image_id}/quarantine')
    async def illustration_quarantine(image_id: int, quarantined: bool = True, conn: Any = Conn) -> dict:
        if not await illustrations.set_quarantine(conn, image_id, quarantined):
            raise HTTPException(404, 'not_found')
        return dict(ok=True, quarantined=quarantined)

    async def _planner_ready() -> bool | None:
        """Отвечает ли локальная нейросеть, описывающая сцены (без неё бесплатные генераторы пропускаются). None — адрес не проверялся."""
        import os
        from urllib.parse import urlsplit

        import httpx
        url = urlsplit(os.getenv('PRAYER_AI_URL', 'http://prayer-ai:8080'))
        if url.scheme != 'http' or url.hostname not in {'prayer-ai', '127.0.0.1', 'localhost'}:
            return None
        try:
            async with httpx.AsyncClient(timeout=2, follow_redirects=False) as http:
                return (await http.get(f'http://{url.hostname}:{url.port or 80}/health')).status_code == 200
        except httpx.HTTPError:
            return False

    def _secrets_ready() -> bool:
        """Можно ли сохранять ключи (есть ключ шифрования)."""
        try:
            secrets_box.encrypt('probe')
            return True
        except RuntimeError:
            return False

    @router.post('/image-providers', status_code=201)
    async def add_image_provider(payload: ProviderIn, conn: Any = Conn) -> dict:
        """Добавить генератор; адрес проверяется (HTTPS, не локальная сеть), ключ сохраняется зашифрованным."""
        spec = payload.model_dump()
        try:
            free_image_providers.validate_spec(spec)
        except ValueError as error:
            raise HTTPException(422, 'invalid_' + str(error)) from None
        if payload.api_key:
            need_secrets()
        if await conn.fetchval('SELECT 1 FROM image_custom_providers WHERE slug=$1', payload.slug):
            raise HTTPException(409, 'slug_taken')
        import json
        row = await conn.fetchrow('''INSERT INTO image_custom_providers(slug,label,adapter,base_url,model,api_key_enc,account_id,width,height,
            steps,priority,daily_limit,is_enabled,extra) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb) RETURNING *''',
            payload.slug, payload.label, payload.adapter, payload.base_url.rstrip('/'), payload.model, secrets_box.encrypt(payload.api_key),
            payload.account_id, payload.width, payload.height, payload.steps, payload.priority, payload.daily_limit, payload.is_enabled,
            json.dumps(payload.extra))
        return public(row)

    @router.patch('/image-providers/{provider_id}')
    async def patch_image_provider(provider_id: int, payload: ProviderPatch, conn: Any = Conn) -> dict:
        """Изменить настройки генератора (ключ заменяется только если передан новый)."""
        import json
        row = await conn.fetchrow('SELECT * FROM image_custom_providers WHERE id=$1', provider_id)
        if not row:
            raise HTTPException(404, 'not_found')
        values = payload.model_dump(exclude_none=True, exclude={'api_key', 'clear_key'})
        spec = dict(row, **values)
        try:
            free_image_providers.validate_spec(spec)
        except ValueError as error:
            raise HTTPException(422, 'invalid_' + str(error)) from None
        if payload.api_key:
            need_secrets()
            values['api_key_enc'] = secrets_box.encrypt(payload.api_key)
        elif payload.clear_key:
            values['api_key_enc'] = ''
        if 'base_url' in values:
            values['base_url'] = values['base_url'].rstrip('/')
        if not values:
            raise HTTPException(422, 'nothing_to_change')
        if 'extra' in values:
            values['extra'] = json.dumps(values['extra'])
        names = list(values)                                                  # имена столбцов берутся только из модели выше
        sets = ','.join(f'{n}=${i + 2}' + ('::jsonb' if n == 'extra' else '') for i, n in enumerate(names))
        updated = await conn.fetchrow(f'UPDATE image_custom_providers SET {sets},updated_at=now() WHERE id=$1 RETURNING *',  # noqa: S608
                                      provider_id, *values.values())
        return public(updated)

    @router.delete('/image-providers/{provider_id}')
    async def delete_image_provider(provider_id: int, conn: Any = Conn) -> dict:
        """Удалить генератор (история попыток сохраняется)."""
        if await conn.execute('DELETE FROM image_custom_providers WHERE id=$1', provider_id) != 'DELETE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    @router.post('/image-providers/{provider_id}/test')
    async def test_image_provider(provider_id: int, conn: Any = Conn) -> dict:
        """Проверочная генерация (не учитывается в лимитах заданий); возвращает уменьшенный просмотр и результат."""
        row = await conn.fetchrow('SELECT * FROM image_custom_providers WHERE id=$1', provider_id)
        if not row:
            raise HTTPException(404, 'not_found')
        try:
            key = secrets_box.decrypt(row['api_key_enc'])
        except (ValueError, RuntimeError):
            raise HTTPException(409, 'secret_unreadable') from None
        import json
        spec = dict(row, api_key=key, extra=json.loads(row['extra']) if isinstance(row['extra'], str) else row['extra'])
        started = time.monotonic()
        error_code, preview, size = '', None, 0
        try:
            data, _, _ = await free_image_providers.generate(spec, TEST_PROMPT)
            size = len(data)
            with Image.open(BytesIO(data)) as image:
                image.thumbnail((240, 240))
                out = BytesIO()
                image.convert('RGB').save(out, format='JPEG', quality=70)
            preview = base64.b64encode(out.getvalue()).decode()
        except GenerationError as error:
            error_code = error.code
        except Exception as error:
            error_code = type(error).__name__
        await conn.execute('UPDATE image_custom_providers SET last_test_at=now(),last_test_ok=$2,last_test_error=$3 WHERE id=$1',
                           provider_id, not error_code, error_code)
        return dict(ok=not error_code, error=error_code, bytes=size, seconds=round(time.monotonic() - started, 1), preview=preview)

    @router.get('/image-queue')
    async def image_queue(conn: Any = Conn) -> dict:
        """Состояние очереди иллюстраций и последние ошибки по заданиям."""
        jobs = {r['state']: r['n'] for r in await conn.fetch('SELECT state,count(*) AS n FROM image_generation_jobs GROUP BY state')}
        recent = await conn.fetch('''SELECT id,state,error_code,attempts,updated_at FROM image_generation_jobs
            WHERE state IN ('failed','uncertain') ORDER BY updated_at DESC LIMIT 20''')
        return dict(jobs=jobs, failures=[dict(r, updated_at=r['updated_at'].isoformat()) for r in recent])

    @router.post('/image-queue/retry')
    async def image_retry(conn: Any = Conn) -> dict:
        """Вернуть в очередь задания, остановленные из-за отсутствия провайдеров или исчерпания цепочки (платно «неопределённые» не трогаются)."""
        result = await conn.execute("""UPDATE image_generation_jobs SET state='retry',retry_at=NULL,updated_at=now()
            WHERE state='failed' AND error_code IN ('providers_exhausted','missing_key','auth','quota')""")
        return {'requeued': int(result.split()[-1])}

    # --- Настройки разделов -----------------------------------------------------------------------
    @router.get('/settings')
    async def get_settings(conn: Any = Conn) -> dict:
        """Текущие значения настраиваемых разделов и источник значения."""
        stored = await conn.fetchval('SELECT value FROM app_settings WHERE key=$1', features.SUPPORT_KEY)
        await features.refresh(conn)
        return dict(support_section=features.support_enabled(), support_source='panel' if stored is not None else 'env')

    @router.put('/settings')
    async def put_settings(payload: SettingsIn, conn: Any = Conn) -> dict:
        """Изменить настройки разделов; бот подхватывает значение не позднее чем через 30 секунд."""
        if payload.support_section is None:
            raise HTTPException(422, 'nothing_to_change')
        await features.set_support(conn, payload.support_section)
        return dict(support_section=payload.support_section, support_source='panel')

    # --- Чаты и пользователи ----------------------------------------------------------------------
    @router.get('/chats')
    async def list_chats(platform: str = '', q: str = '', conn: Any = Conn) -> list[dict]:
        """Назначения (личные чаты, группы, каналы) с фильтром по платформе и поиском по названию."""
        rows = await conn.fetch('''SELECT telegram_chat_id AS id,platform,chat_type,COALESCE(title,'') AS title,COALESCE(username,'') AS username,
            ui_language,timezone,is_active,updated_at FROM telegram_chats
            WHERE ($1='' OR platform=$1) AND ($2='' OR title ILIKE '%'||$2||'%' OR username ILIKE '%'||$2||'%')
            ORDER BY updated_at DESC LIMIT 200''', platform, q[:50])
        return [dict(r, updated_at=r['updated_at'].isoformat()) for r in rows]

    @router.put('/chats/{chat_id}/active')
    async def set_chat_active(chat_id: int, payload: FlagIn, conn: Any = Conn) -> dict:
        """Включить или отключить назначение (при отключении подписки приостанавливаются)."""
        async with conn.transaction():
            if await conn.execute('UPDATE telegram_chats SET is_active=$2 WHERE telegram_chat_id=$1', chat_id, payload.value) != 'UPDATE 1':
                raise HTTPException(404, 'not_found')
            if not payload.value:
                await conn.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE telegram_chat_id=$1', chat_id)
        return {'ok': True}

    @router.get('/users')
    async def list_users(q: str = '', conn: Any = Conn) -> list[dict]:
        """Пользователи: администраторы и заблокированные первыми; поиск по имени."""
        rows = await conn.fetch('''SELECT telegram_user_id AS id,platform,COALESCE(username,'') AS username,COALESCE(first_name,'') AS first_name,
            is_owner,is_admin,is_blocked FROM telegram_users
            WHERE $1='' OR first_name ILIKE '%'||$1||'%' OR username ILIKE '%'||$1||'%'
            ORDER BY is_owner DESC,is_admin DESC,is_blocked DESC,updated_at DESC LIMIT 200''', q[:50])
        return [dict(r) for r in rows]

    @router.put('/users/{user_id}/blocked')
    async def set_user_blocked(user_id: int, payload: FlagIn, conn: Any = Conn) -> dict:
        """Заблокировать или разблокировать пользователя (владельца заблокировать нельзя)."""
        if await conn.execute('UPDATE telegram_users SET is_blocked=$2 WHERE telegram_user_id=$1 AND NOT is_owner', user_id, payload.value) != 'UPDATE 1':
            raise HTTPException(404, 'not_found')
        return {'ok': True}

    # --- Доставка ---------------------------------------------------------------------------------
    @router.get('/deliveries/attention')
    async def deliveries_attention(conn: Any = Conn) -> list[dict]:
        """Доставки, которым нужно решение: неудачные и с неопределённым исходом."""
        rows = await conn.fetch('''SELECT id,telegram_chat_id AS chat_id,mode,status,error_code,attempt_count,updated_at FROM delivery_log
            WHERE status IN ('failed','uncertain') ORDER BY updated_at DESC LIMIT 100''')
        return [dict(r, updated_at=r['updated_at'].isoformat()) for r in rows]

    @router.post('/deliveries/{delivery_id}/resolve')
    async def resolve_delivery(delivery_id: int, payload: ResolveIn, conn: Any = Conn) -> dict:
        """Решение оператора: сообщение дошло, повторить (возможен дубль) или отменить; действие записывается в журнал."""
        chat_id = await conn.fetchval('SELECT telegram_chat_id FROM delivery_log WHERE id=$1', delivery_id)
        if chat_id is None:
            raise HTTPException(404, 'not_found')
        await resolve_uncertain(conn, delivery_id, chat_id, await owner_id(conn), payload.action)
        return {'ok': True}

    return router

