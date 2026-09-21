"""Read-only, locally bound operational dashboard. Settings are changed in Telegram."""
from __future__ import annotations
import json
import secrets
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated,Any
from fastapi import Depends,FastAPI,Header,HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic,HTTPBasicCredentials
from app.config import Settings
from app.db import close_pool,create_pool,wait_for_database,acquire_runtime_guard
from app.services.verification import verify_database

settings = Settings.from_env(require_bot_token=False)
security = HTTPBasic()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Connect at startup, close pooled sockets on graceful termination."""
    await wait_for_database(settings)
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as owner:
            await acquire_runtime_guard(owner)
            yield
    finally:
        await close_pool()


app = FastAPI(title='BibleMessengerBot read-only operations',version='1.2.0',lifespan=lifespan,
              docs_url=None,redoc_url=None,openapi_url=None)


def valid_key(value: str) -> bool:
    """An empty/default key cannot authenticate the operator."""
    return len(settings.admin_api_key)>=24 and secrets.compare_digest(value,settings.admin_api_key)


def require_basic(credentials: Annotated[HTTPBasicCredentials,Depends(security)]) -> None:
    """Expose the dashboard only through a local listener or authenticated SSH tunnel."""
    if credentials.username!='admin' or not valid_key(credentials.password):
        raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Basic'})


def require_key(x_admin_key: Annotated[str | None,Header()] = None) -> None:
    """JSON operator API uses a header, never a query-string secret."""
    if not x_admin_key or not valid_key(x_admin_key):
        raise HTTPException(401,'Authentication required')


@app.get('/health')
async def health() -> dict[str,str]:
    """Liveness does not claim that imported content is ready."""
    return {'status':'alive','version':'1.2.0'}


@app.get('/ready')
async def ready() -> dict[str,Any]:
    """Readiness is a real database query; empty/unaudited required content returns 503."""
    try:
        pool = await create_pool(settings)
        async with pool.acquire() as connection:
            report = await verify_database(connection,profile=settings.bible_profile,full=False)
    except Exception:
        raise HTTPException(503,'Database unavailable') from None
    if report['status']!='passed':
        raise HTTPException(503,{'status':'not_ready','errors':report['errors']})
    return {'status':'ready','editions':report['edition_count'],'languages':report['language_count']}


async def operator_data() -> dict[str,Any]:
    """Bounded status output; message text, usernames and bot credentials are omitted."""
    pool = await create_pool(settings)
    async with pool.acquire() as connection:
        audit = await verify_database(connection,profile=settings.bible_profile,full=False)
        chats = await connection.fetch('SELECT telegram_chat_id,chat_type,ui_language,bible_language_code,default_translation_id,timezone,is_active FROM telegram_chats ORDER BY updated_at DESC LIMIT 200')
        subscriptions = await connection.fetch('SELECT id,telegram_chat_id,mode,send_time,timezone,is_enabled,plan_day,completed,next_run_at FROM subscriptions ORDER BY id DESC LIMIT 200')
        deliveries = await connection.fetch('SELECT id,telegram_chat_id,subscription_id,status,next_chunk,jsonb_array_length(chunks) AS chunks,error_code,updated_at FROM delivery_log ORDER BY id DESC LIMIT 100')
        heartbeats = await connection.fetch('SELECT service,last_seen FROM service_heartbeats')
    return {'version':'1.2.0','database':audit,'chats':[dict(r) for r in chats],
        'subscriptions':[dict(r) for r in subscriptions],'deliveries':[dict(r) for r in deliveries],
        'heartbeats':[dict(r) for r in heartbeats]}


@app.get('/api/stats',dependencies=[Depends(require_key)])
async def api_stats() -> dict[str,Any]:
    """Read-only API; no CSRF-capable form mutation endpoints are exposed."""
    return await operator_data()


@app.get('/admin',response_class=HTMLResponse,dependencies=[Depends(require_basic)])
async def dashboard() -> HTMLResponse:
    """Escaped preformatted diagnostic output, without scripts or external resources."""
    data = json.dumps(await operator_data(),ensure_ascii=False,indent=2,default=str)
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>BibleMessengerBot · Operations</title><h1>BibleMessengerBot 1.2.0</h1>'
        '<p>Read-only operations. Change destination settings through /settings in Telegram.</p><pre>'+
        escape(data)+'</pre></html>',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
