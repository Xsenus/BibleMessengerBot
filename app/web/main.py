"""Локальная панель оператора: только чтение, слушает только локальный интерфейс. Настройки меняются в Telegram."""
from __future__ import annotations
import json
import os
import time
from decimal import Decimal
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated,Any
from fastapi import Depends,FastAPI,Header,HTTPException,Request,Response
from fastapi.responses import HTMLResponse,JSONResponse,PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic,HTTPBasicCredentials
from app.config import Settings
from app.db import close_pool,create_pool,wait_for_database,acquire_runtime_guard
from app.services.errors import UserError
from app.services.metrics import collect as collect_metrics
from app.services.verification import verify_database

settings = Settings.from_env(require_bot_token=False)
security = HTTPBasic()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Жизненный цикл приложения: при старте подключиться к БД и взять разделяемую блокировку обслуживания, при остановке закрыть пул соединений."""
    await wait_for_database(settings)
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            yield
    finally:
        await close_pool()


app = FastAPI(title='BibleMessengerBot read-only operations',version='1.20.0',lifespan=lifespan,
              docs_url=None,redoc_url=None,openapi_url=None)


def valid_key(value: str) -> bool:
    """Проверить ключ администратора: длина не менее 24 символов и постоянное по времени сравнение; пустой или стандартный ключ войти не позволяет."""
    return len(settings.admin_api_key)>=24 and secrets.compare_digest(value.encode('utf-8'),settings.admin_api_key.encode('utf-8'))


def require_basic(credentials: Annotated[HTTPBasicCredentials,Depends(security)]) -> None:
    """Зависимость FastAPI: требует HTTP Basic (пользователь `admin`, пароль — ключ администратора)."""
    if credentials.username!='admin' or not valid_key(credentials.password):
        raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Basic'})


def require_key(x_admin_key: Annotated[str | None,Header()] = None) -> None:
    """Зависимость FastAPI: требует ключ администратора в заголовке `X-Admin-Key` (не в строке запроса)."""
    if not x_admin_key or not valid_key(x_admin_key):
        raise HTTPException(401,'Authentication required')


from app.web.api import build_router  # noqa: E402
from app.web.api_net import build_router as build_net_router  # noqa: E402
from app.web.api_system import build_router as build_system_router  # noqa: E402  (нужны require_key и settings, определённые выше)

@app.exception_handler(UserError)
async def user_error(_request: Request, error: UserError) -> JSONResponse:
    """Ожидаемая ошибка сервиса → 403/404/422 с кодом без внутренних подробностей."""
    return JSONResponse({'detail': error.key}, status_code={'forbidden': 403, 'no_result': 404}.get(error.key, 422))


app.include_router(build_router(require_key, lambda: create_pool(settings)))
app.include_router(build_system_router(require_key, lambda: create_pool(settings)))
app.include_router(build_net_router(require_key, lambda: create_pool(settings)))
from app.web.api_ops import build_router as build_ops_router  # noqa: E402
app.include_router(build_ops_router(require_key, lambda: create_pool(settings)))


PANEL_DIR = Path(__file__).parent / 'static' / 'panel'


@app.middleware('http')
async def panel_headers(request: Request, call_next):
    """Заголовки безопасности для статической панели и API: без кэша, без вложения в чужие страницы, скрипты только свои."""
    response = await call_next(request)
    if (request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.url.path.startswith('/api/admin')
            and response.status_code < 400 and not request.url.path.endswith('/preview')):
        try:
            from app.services import audit
            pool = await create_pool(settings)
            async with pool.acquire() as connection:
                await audit.record(connection, 'panel', 'panel', f'{request.method} {request.url.path[10:]}'[:100])
        except Exception:
            pass                                             # журнал не должен ломать ответ
    if request.url.path.startswith(('/panel', '/api/admin')):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Content-Security-Policy',
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith('/api/admin'):
            response.headers['Cache-Control'] = 'no-store'
    return response


if PANEL_DIR.is_dir():
    app.mount('/panel', StaticFiles(directory=PANEL_DIR, html=True), name='panel')


@app.get('/health')
async def health() -> dict[str,str]:
    """Проверка «жив»: не утверждает, что корпус Библии готов."""
    return {'status':'alive','version':'1.20.0'}


@app.get('/ready')
async def ready() -> dict[str,Any]:
    """Проверка готовности: реальный запрос к БД и аудит корпуса; если БД недоступна или нет нужных изданий, ответ 503."""
    try:
        pool = await create_pool(settings)
        async with pool.acquire() as connection:
            report = await verify_database(connection,profile=settings.bible_profile,full=False)
    except Exception:
        raise HTTPException(503,'Database unavailable') from None
    if report['status']!='passed':
        raise HTTPException(503,{'status':'not_ready'})
    return {'status':'ready','editions':report['edition_count'],'languages':report['language_count']}


async def operator_data() -> dict[str,Any]:
    """Ограниченный по размеру срез состояния для оператора: аудит БД, чаты, подписки, доставки, сигналы служб, подготовка чтений и молитвы. Текст сообщений, имена пользователей и токены не включаются."""
    pool = await create_pool(settings)
    async with pool.acquire() as connection:
        audit = await verify_database(connection,profile=settings.bible_profile,full=False)
        chats = await connection.fetch('SELECT telegram_chat_id,chat_type,ui_language,bible_language_code,default_translation_id,timezone,is_active FROM telegram_chats ORDER BY updated_at DESC LIMIT 200')
        subscriptions = await connection.fetch('SELECT id,telegram_chat_id,mode,send_time,timezone,is_enabled,plan_day,completed,next_run_at FROM subscriptions ORDER BY id DESC LIMIT 200')
        deliveries = await connection.fetch('SELECT id,telegram_chat_id,subscription_id,status,next_chunk,jsonb_array_length(chunks) AS chunks,error_code,updated_at FROM delivery_log ORDER BY id DESC LIMIT 100')
        heartbeats = await connection.fetch('SELECT service,last_seen FROM service_heartbeats')
        preparations=await connection.fetch("SELECT state,count(*) AS count FROM scheduled_readings GROUP BY state")
        prayers=await connection.fetch('SELECT id,scheduled_for,state,delivery_id FROM prayer_occurrences ORDER BY scheduled_for DESC LIMIT 100')
    return {'version':'1.20.0','database':audit,'chats':[dict(r) for r in chats],
        'subscriptions':[dict(r) for r in subscriptions],'deliveries':[dict(r) for r in deliveries],
        'heartbeats':[dict(r) for r in heartbeats],'preparations':[dict(r) for r in preparations],
        'prayers':[dict(r) for r in prayers]}


@app.get('/api/stats',dependencies=[Depends(require_key)])
async def api_stats(response: Response) -> dict[str,Any]:
    """JSON-версия среза состояния (по ключу администратора), без кэширования."""
    response.headers['Cache-Control'] = 'no-store'
    return await operator_data()


METRICS_TTL_SECONDS = 10.0
_metrics_cache: list = [0.0,'']  # [expires_at (monotonic), rendered text]: bounds DB load from frequent scrapes


def settings_budget() -> Decimal:
    """Месячный бюджет картинок в долларах из IMAGE_MONTHLY_BUDGET_USD (по умолчанию 10)."""
    return Decimal(os.getenv('IMAGE_MONTHLY_BUDGET_USD','10'))


@app.get('/metrics',response_class=PlainTextResponse,dependencies=[Depends(require_basic)])
async def metrics() -> PlainTextResponse:
    """Метрики Prometheus (HTTP Basic). Результат кэшируется на 10 секунд, поэтому частые опросы не нагружают БД."""
    now = time.monotonic()
    if _metrics_cache[0] < now:
        pool = await create_pool(settings)
        async with pool.acquire() as connection:
            _metrics_cache[1] = (await collect_metrics(connection,settings_budget())).render()
        _metrics_cache[0] = now + METRICS_TTL_SECONDS
    body = _metrics_cache[1]
    return PlainTextResponse(body,media_type='text/plain; version=0.0.4',headers={'Cache-Control':'no-store'})


@app.get('/admin',response_class=HTMLResponse,dependencies=[Depends(require_basic)])
async def dashboard() -> HTMLResponse:
    """HTML-страница оператора: экранированный JSON-срез без скриптов и внешних ресурсов, с жёсткой политикой безопасности."""
    data = json.dumps(await operator_data(),ensure_ascii=False,indent=2,default=str)
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>BibleMessengerBot · Operations</title><h1>BibleMessengerBot 1.20.0</h1>'
        '<p>Read-only operations. Change destination settings through /settings in Telegram.</p><pre>'+
        escape(data)+'</pre></html>',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
