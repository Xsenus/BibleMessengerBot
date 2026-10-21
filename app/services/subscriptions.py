"""Проверенные подписки с ревизиями и настоящей длительностью планов."""
from __future__ import annotations
import json
from datetime import time
from typing import Any
from app.services.scheduling import next_reading, validate_timezone
from app.services.plans import PLANS, validate_plan
from app.services.errors import UserError
from app.services.locks import chat_lock
from app.services.destinations import ensure_resolved, configure_chat

async def cancel_subscription_jobs(connection: Any, subscription_id: int) -> None:
    """Отменить только задания этой подписки (другие режимы и их контрольные точки не трогаются)."""
    await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='subscription_changed',updated_at=now() WHERE subscription_id=$1 AND status IN ('pending','retry')", subscription_id)


VALID_MODES = {'sequential','verse_of_day','topic_of_day','reading_plan','morning_verse','evening_verse'}


async def create_or_update_subscription(connection: Any, *, chat_id: int, created_by: int | None,
    translation_id: int, mode: str, send_time: time, timezone_name: str,
    plan_code: str | None = None) -> Any:
    """Создать или обновить подписку чата: проверить режим, пояс, план и издание, при необходимости привести настройки чата, отменить старые снимки и увеличить ревизию. План сбрасывается только при смене плана или издания; завершённая подписка без сброса не меняется. Возвращает строку подписки."""
    if mode not in VALID_MODES:
        raise UserError('invalid')
    validate_timezone(timezone_name)
    if mode == 'reading_plan':
        plan_code = plan_code or 'bible-365'
        if plan_code not in PLANS:
            raise UserError('invalid')
    else:
        plan_code = None
    next_run = next_reading(mode,send_time,timezone_name)
    async with chat_lock(connection,chat_id), connection.transaction():
        await ensure_resolved(connection,chat_id)
        translation = await connection.fetchrow("""SELECT * FROM translations WHERE id=$1
            AND is_active AND audit_status IN ('passed','passed_with_warnings')""",translation_id)
        if not translation:
            raise UserError('not_ready')
        if mode=='topic_of_day' and not translation.get('numbering_system','BibleNLP Original versification').startswith('BibleNLP'):
            raise UserError('no_result','No verified thematic reference mapping for this numbering system')
        if plan_code:
            await validate_plan(connection,translation,plan_code)
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat_id)
        if not chat:
            raise UserError('no_result')
        if chat['default_translation_id'] != translation_id or chat['timezone'] != timezone_name:
            await configure_chat(connection,chat_id=chat_id,actor_id=created_by,
                translation_id=translation_id,timezone_name=timezone_name)
        old = await connection.fetchrow('SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND mode=$2 FOR UPDATE',chat_id,mode)
        reset = bool(old and (old['plan_code'] != plan_code or old['translation_id'] != translation_id))
        if old and old['completed'] and not reset:
            raise UserError('complete')
        if old:
            await cancel_subscription_jobs(connection,old["id"])
        row = await connection.fetchrow('''INSERT INTO subscriptions(telegram_chat_id,created_by,translation_id,mode,
            send_time,timezone,plan_code,next_run_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(telegram_chat_id,mode) DO UPDATE SET created_by=EXCLUDED.created_by,
            translation_id=EXCLUDED.translation_id,send_time=EXCLUDED.send_time,timezone=EXCLUDED.timezone,
            plan_code=EXCLUDED.plan_code,next_run_at=EXCLUDED.next_run_at,is_enabled=true,paused_reason=NULL,
            revision=subscriptions.revision+1,retry_at=NULL,
            plan_day=CASE WHEN $9 THEN 0 ELSE subscriptions.plan_day END,
            current_book_code=CASE WHEN $9 THEN NULL ELSE subscriptions.current_book_code END,
            current_chapter=CASE WHEN $9 THEN NULL ELSE subscriptions.current_chapter END,
            completed=CASE WHEN $9 THEN false ELSE subscriptions.completed END,updated_at=now() RETURNING *''',
            chat_id,created_by,translation_id,mode,send_time,timezone_name,plan_code,next_run,reset)
        # The chat owns the schedule timezone used by manual /today as well.
        await connection.execute('UPDATE telegram_chats SET timezone=$2,default_translation_id=$3,updated_at=now() WHERE telegram_chat_id=$1',chat_id,timezone_name,translation_id)
    return row


async def set_enabled(connection: Any, chat_id: int, enabled: bool, mode: str | None = None) -> int:
    """Включить или приостановить подписки (все или один режим). При возобновлении вычисляется будущее время, прогресс сохраняется, завершённые планы пропускаются; незавершённые доставки требуют решения оператора."""
    if mode is not None and mode not in VALID_MODES:
        raise UserError('invalid')
    async with chat_lock(connection,chat_id), connection.transaction():
        if enabled and await connection.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE telegram_chat_id=$1 AND (status IN ('sending','uncertain') OR (status='failed' AND next_chunk>0)))",chat_id):
            raise UserError('pending_review')
        rows = await connection.fetch('SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND ($2::text IS NULL OR mode=$2)',chat_id,mode)
        count = 0
        for row in rows:
            if enabled and row['completed']:
                continue
            next_run = next_reading(row['mode'],row['send_time'],row['timezone'],list(row['days_of_week'])) if enabled else None
            await connection.execute('''UPDATE subscriptions SET is_enabled=$2,next_run_at=$3,paused_reason=NULL,
                updated_at=now() WHERE id=$1''',row['id'],enabled,next_run)
            count += 1
    return count


BOT_UNAVAILABLE = 'bot_unavailable'
REVIEW_PENDING = "SELECT EXISTS(SELECT 1 FROM delivery_log WHERE telegram_chat_id=$1 AND (status IN ('sending','uncertain') OR (status='failed' AND next_chunk>0)))"


async def pause_unavailable(connection: Any, chat_id: int, cause: str) -> int:
    """Бота остановили, заблокировали или удалили из чата: выключить включённые подписки с пометкой, что их можно
    вернуть сами, и записать событие. Уже выключенные вручную не трогаются. Вызывать под chat_lock в транзакции."""
    result = await connection.execute("""UPDATE subscriptions SET is_enabled=false,next_run_at=NULL,paused_reason=$2,updated_at=now()
        WHERE telegram_chat_id=$1 AND is_enabled""", chat_id, BOT_UNAVAILABLE)
    count = int(result.split()[-1])
    if count:
        await connection.execute("INSERT INTO operator_events(chat_id,action,details) VALUES($1,'subscriptions_paused',$2::jsonb)",
                                 chat_id, json.dumps({'reason': BOT_UNAVAILABLE, 'cause': cause, 'count': count}))
    return count


async def resume_paused(connection: Any, chat_id: int, cause: str, *, any_reason: bool = False) -> int:
    """Снова включить выключенные подписки чата: по умолчанию только выключенные из-за недоступности бота,
    с any_reason — все незавершённые. Задания, срок которых прошёл за время паузы, пропускаются, чтобы не прислать
    пачку старых сообщений. При неопределённой отправке ничего не включается (нужно решение оператора).
    Вызывать под chat_lock в транзакции. Возвращает число включённых."""
    if await connection.fetchval(REVIEW_PENDING, chat_id):
        return 0
    rows = await connection.fetch("""SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND NOT is_enabled AND NOT completed
        AND ($2 OR paused_reason=$3) ORDER BY id""", chat_id, any_reason, BOT_UNAVAILABLE)
    if not rows:
        return 0
    ids = [row['id'] for row in rows]
    await connection.execute("""UPDATE delivery_log SET status='skipped',error_code='stale_after_pause',updated_at=now()
        WHERE subscription_id=ANY($1::bigint[]) AND status IN ('pending','retry') AND scheduled_for<now()""", ids)
    for row in rows:
        next_run = next_reading(row['mode'],row['send_time'],row['timezone'],list(row['days_of_week']))
        await connection.execute('UPDATE subscriptions SET is_enabled=true,next_run_at=$2,paused_reason=NULL,updated_at=now() WHERE id=$1',
                                 row['id'], next_run)
    await connection.execute("INSERT INTO operator_events(chat_id,action,details) VALUES($1,'subscriptions_resumed',$2::jsonb)",
                             chat_id, json.dumps({'cause': cause, 'modes': [row['mode'] for row in rows]}))
    return len(rows)


def resumed_notice(locale: str | None) -> str:
    """Сообщение в чат после автоматического возврата рассылок."""
    if (locale or 'ru') == 'ru':
        return '🔔 Рассылки снова включены — бот продолжит присылать их по прежнему расписанию. Приостановить: /pause'
    return '🔔 Your subscriptions are back on and will follow the previous schedule. Pause them with /pause'

async def delete_subscriptions(connection: Any, chat_id: int, mode: str | None = None) -> int:
    """Удалить подписки (все или один режим) и отменить их задания; опечатка в режиме не удаляет всё. Отписка возможна всегда, в том числе после неопределённой отправки. Возвращает число удалённых."""
    if mode is not None and mode not in VALID_MODES:
        raise UserError('invalid')
    async with chat_lock(connection,chat_id), connection.transaction():
        # Unsubscribe must always be possible, including after an ambiguous send.
        rows = await connection.fetch('SELECT id FROM subscriptions WHERE telegram_chat_id=$1 AND ($2::text IS NULL OR mode=$2)',chat_id,mode)
        for row in rows:
            await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='unsubscribed',updated_at=now() WHERE subscription_id=$1 AND status IN ('pending','retry','failed','uncertain')",row['id'])
        if mode is None:
            await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='unsubscribed',updated_at=now() WHERE telegram_chat_id=$1 AND mode='manual' AND status IN ('pending','retry','failed','uncertain')",chat_id)
        result = await connection.execute('DELETE FROM subscriptions WHERE telegram_chat_id=$1 AND ($2::text IS NULL OR mode=$2)',chat_id,mode)
    return int(result.split()[-1])


async def list_subscriptions(connection: Any, chat_id: int) -> list[Any]:
    """Подписки одного чата с названиями изданий."""
    return await connection.fetch('''SELECT s.*,t.title AS translation_title,t.source_translation_id,t.source_name AS translation_source_name
        FROM subscriptions s JOIN translations t ON t.id=s.translation_id
        WHERE s.telegram_chat_id=$1 ORDER BY s.mode''',chat_id)
