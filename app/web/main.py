"""FastAPI health endpoints and a compact operator dashboard."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import time
from pathlib import Path
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.db import close_pool, create_pool, wait_for_database
from app.logging import configure_logging
from app.services.scheduling import parse_hhmm, validate_timezone
from app.services.subscriptions import create_or_update_subscription

configure_logging()
settings = Settings.from_env(require_bot_token=False)
security = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await wait_for_database(settings)
    await create_pool(settings)
    yield
    await close_pool()


app = FastAPI(title="BibleMessengerBot Admin", version="1.1.0", lifespan=lifespan)


def _valid_key(candidate: str) -> bool:
    return bool(settings.admin_api_key) and secrets.compare_digest(candidate, settings.admin_api_key)


def require_basic(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> None:
    if credentials.username != "admin" or not _valid_key(credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def require_api_key(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    if not x_admin_key or not _valid_key(x_admin_key):
        raise HTTPException(status_code=401, detail="Invalid X-Admin-Key")


async def _pool() -> asyncpg.Pool:
    return await create_pool(settings)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "1.1.0"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as connection:
        translations = await connection.fetchval(
            "SELECT COUNT(*) FROM translations WHERE is_active=true AND verse_count>0"
        )
        verses = await connection.fetchval("SELECT COUNT(*) FROM verses")
    status_value = "ready" if translations and verses else "initializing"
    return {"status": status_value, "translations": translations, "verses": verses}


async def _dashboard_data(connection: asyncpg.Connection) -> dict[str, Any]:
    stats = {
        "languages": await connection.fetchval("SELECT COUNT(*) FROM languages"),
        "translations": await connection.fetchval(
            "SELECT COUNT(*) FROM translations WHERE is_active=true"
        ),
        "verses": await connection.fetchval("SELECT COUNT(*) FROM verses"),
        "chats": await connection.fetchval("SELECT COUNT(*) FROM telegram_chats"),
        "subscriptions": await connection.fetchval("SELECT COUNT(*) FROM subscriptions"),
        "sent": await connection.fetchval("SELECT COUNT(*) FROM delivery_log WHERE status='sent'"),
    }
    translations = await connection.fetch(
        """
        SELECT t.id, l.code AS language, t.source_translation_id, t.title,
               t.coverage, t.book_count, t.verse_count, t.license_type,
               left(t.source_sha256, 12) AS checksum
        FROM translations t JOIN languages l ON l.id=t.language_id
        WHERE t.is_active=true ORDER BY l.code, t.title LIMIT 500
        """
    )
    subscriptions = await connection.fetch(
        """
        SELECT s.id, s.telegram_chat_id, c.title, s.mode, s.send_time,
               s.timezone, s.is_enabled, s.next_run_at,
               t.source_translation_id
        FROM subscriptions s
        JOIN telegram_chats c ON c.telegram_chat_id=s.telegram_chat_id
        JOIN translations t ON t.id=s.translation_id
        ORDER BY s.next_run_at NULLS LAST LIMIT 200
        """
    )
    imports = await connection.fetch(
        """
        SELECT id, profile, status, selected_count, imported_count,
               skipped_count, failed_count, started_at, finished_at
        FROM import_runs ORDER BY id DESC LIMIT 20
        """
    )
    chats = await connection.fetch(
        """
        SELECT telegram_chat_id, chat_type, title, username, is_active
        FROM telegram_chats ORDER BY updated_at DESC LIMIT 200
        """
    )
    return {
        "stats": stats,
        "translations": translations,
        "subscriptions": subscriptions,
        "imports": imports,
        "chats": chats,
    }


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_basic)])
async def dashboard(request: Request) -> HTMLResponse:
    pool = await _pool()
    async with pool.acquire() as connection:
        data = await _dashboard_data(connection)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"version": "1.1.0", **data},
    )


@app.post("/admin/subscription", dependencies=[Depends(require_basic)])
async def admin_subscription(
    chat_id: Annotated[int, Form()],
    translation_id: Annotated[int, Form()],
    mode: Annotated[str, Form()],
    send_time: Annotated[str, Form()],
    timezone_name: Annotated[str, Form()],
) -> RedirectResponse:
    parsed_time: time = parse_hhmm(send_time)
    validate_timezone(timezone_name)
    pool = await _pool()
    async with pool.acquire() as connection:
        chat_exists = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM telegram_chats WHERE telegram_chat_id=$1)", chat_id
        )
        if not chat_exists:
            raise HTTPException(400, "Chat is not registered in the bot")
        await create_or_update_subscription(
            connection,
            chat_id=chat_id,
            created_by=None,
            translation_id=translation_id,
            mode=mode,
            send_time=parsed_time,
            timezone_name=timezone_name,
        )
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/stats", dependencies=[Depends(require_api_key)])
async def api_stats() -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as connection:
        data = await _dashboard_data(connection)
    return {
        "stats": data["stats"],
        "translations": [dict(row) for row in data["translations"]],
        "subscriptions": [dict(row) for row in data["subscriptions"]],
        "imports": [dict(row) for row in data["imports"]],
    }
