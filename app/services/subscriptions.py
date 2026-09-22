"""Validated subscriptions with revisioned payloads and real plan durations."""
from __future__ import annotations
from datetime import time
from typing import Any
from app.services.scheduling import next_occurrence, validate_timezone
from app.services.plans import PLANS
from app.services.errors import UserError
from app.services.locks import chat_lock
from app.services.destinations import ensure_resolved, configure_chat

async def cancel_subscription_jobs(connection: Any, subscription_id: int) -> None:
    """Cancel only this schedule, leaving other modes and their checkpoints intact."""
    await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='subscription_changed',updated_at=now() WHERE subscription_id=$1 AND status IN ('pending','retry')", subscription_id)


VALID_MODES = {'sequential','verse_of_day','topic_of_day','reading_plan'}


async def create_or_update_subscription(connection: Any, *, chat_id: int, created_by: int | None,
    translation_id: int, mode: str, send_time: time, timezone_name: str,
    plan_code: str | None = None) -> Any:
    """A settings update cancels old snapshots; unchanged plans retain completed days."""
    if mode not in VALID_MODES:
        raise UserError('invalid')
    validate_timezone(timezone_name)
    if mode == 'reading_plan':
        plan_code = plan_code or 'bible-365'
        if plan_code not in PLANS:
            raise UserError('invalid')
    else:
        plan_code = None
    next_run = next_occurrence(send_time,timezone_name)
    async with chat_lock(connection,chat_id), connection.transaction():
        await ensure_resolved(connection,chat_id)
        translation = await connection.fetchrow("""SELECT * FROM translations WHERE id=$1
            AND is_active AND audit_status IN ('passed','passed_with_warnings')""",translation_id)
        if not translation:
            raise UserError('not_ready')
        if mode=='topic_of_day' and not translation.get('numbering_system','BibleNLP Original versification').startswith('BibleNLP'):
            raise UserError('no_result','No verified thematic reference mapping for this numbering system')
        if plan_code and ((plan_code.startswith('bible-') and not translation['canonical_66_complete'])
                 or (plan_code == 'new-testament-90' and not translation['nt_complete'])):
            raise UserError('invalid','The requested plan requires a structurally complete edition')
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
            plan_code=EXCLUDED.plan_code,next_run_at=EXCLUDED.next_run_at,is_enabled=true,
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
    """Resume computes a future occurrence, preserving progress and completed plans."""
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
            next_run = next_occurrence(row['send_time'],row['timezone'],list(row['days_of_week'])) if enabled else None
            await connection.execute('''UPDATE subscriptions SET is_enabled=$2,next_run_at=$3,
                updated_at=now() WHERE id=$1''',row['id'],enabled,next_run)
            count += 1
    return count


async def delete_subscriptions(connection: Any, chat_id: int, mode: str | None = None) -> int:
    """Reject unknown modes instead of deleting every subscription on a typo."""
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
    """List schedules for exactly one destination."""
    return await connection.fetch('''SELECT s.*,t.title AS translation_title,t.source_translation_id
        FROM subscriptions s JOIN translations t ON t.id=s.translation_id
        WHERE s.telegram_chat_id=$1 ORDER BY s.mode''',chat_id)
