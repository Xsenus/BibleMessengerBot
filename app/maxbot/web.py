"""Production MAX webhook receiver: authenticate, bound, validate, persist, then ACK."""
# ruff: noqa: RUF001
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.config import Settings
from app.db import acquire_runtime_guard, close_pool, create_pool, wait_for_database
from app.maxbot.config import MaxSettings
from app.maxbot.events import MAX_EVENT_BYTES, authorized, ingest
from app.payments.ledger import notification
from app.payments.yookassa import MerchantSettings, PaymentError, YooKassa


def create_app(*, config=None, pool=None, payment_api=None) -> FastAPI:
    config = config or MaxSettings.from_env()
    settings = Settings.from_env(require_bot_token=False)

    @asynccontextmanager
    async def lifespan(app):
        merchant = MerchantSettings.from_env()
        app.state.payment_api = payment_api or (YooKassa(merchant) if merchant.enabled else None)
        try:
            if pool is not None:
                app.state.pool = pool
                yield
            else:
                await wait_for_database(settings)
                app.state.pool = await create_pool(settings)
                async with app.state.pool.acquire() as owner:
                    await acquire_runtime_guard(owner)
                    yield
        finally:
            if app.state.payment_api and payment_api is None:
                await app.state.payment_api.close()
            if pool is None:
                await close_pool()

    app = FastAPI(title='Bible Messenger MAX receiver', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    async def health():
        return {'status':'alive','platform':'max'}

    @app.get('/ready')
    async def ready():
        if not config.token or len(config.webhook_secret)<24:
            raise HTTPException(503,'MAX credentials not configured')
        try:
            async with app.state.pool.acquire() as connection:
                present = await connection.fetchval("SELECT to_regclass('max_inbox') IS NOT NULL")
            if not present:
                raise HTTPException(503,'MAX schema unavailable')
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503,'Database unavailable') from None
        return {'status':'ready','role':'webhook_receiver'}

    @app.post('/max/webhook')
    async def webhook(request: Request):
        if not config.token or not config.webhook_secret:
            raise HTTPException(503,'MAX credentials not configured')
        if not authorized(config.webhook_secret,request.headers.get('x-max-bot-api-secret')):
            raise HTTPException(401,'Authentication required')
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                if int(declared)<0 or int(declared)>MAX_EVENT_BYTES:
                    raise HTTPException(413,'Event too large')
            except ValueError:
                raise HTTPException(400,'Invalid content length') from None
        body = bytearray()
        async for portion in request.stream():
            body.extend(portion)
            if len(body)>MAX_EVENT_BYTES:
                raise HTTPException(413,'Event too large')
        try:
            async with app.state.pool.acquire() as connection,connection.transaction():
                await ingest(connection,bytes(body))
        except ValueError:
            raise HTTPException(400,'Invalid MAX event') from None
        # Unexpected DB errors propagate as 5xx: the provider must redeliver.
        return {'success':True}

    @app.post('/max/payments/yookassa')
    async def payment_webhook(request: Request):
        # Basic-auth merchant notifications have no signature. A hint can only
        # cause an authenticated GET of an existing local intent, never mark paid.
        if app.state.payment_api is None:
            raise HTTPException(503, 'Merchant not configured')
        body = bytearray()
        async for portion in request.stream():
            body.extend(portion)
            if len(body) > MAX_EVENT_BYTES:
                raise HTTPException(413, 'Notification too large')
        try:
            event = json.loads(body)
            async with app.state.pool.acquire() as connection:
                await notification(connection, app.state.payment_api, event)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid notification') from None
        except PaymentError:
            # Do not ACK provider outages or mismatches; request redelivery.
            raise HTTPException(503, 'Notification not verified') from None
        return {'success': True}

    @app.get('/max/payments/return')
    async def payment_return():
        from fastapi.responses import HTMLResponse
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>Библия каждый день</title><body>'
                            '<h1>Вернитесь в чат с ботом MAX</h1>'
                            '<p>Бот сообщит результат после проверки платежа. '
                            'Открытие этой страницы само по себе не подтверждает оплату.</p></body></html>',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                     'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'"})

    return app


app = create_app()
