"""Atomic per-chat preferences and cancellation of outdated queued content."""
from __future__ import annotations
import json
from typing import Any
from app.services.errors import UserError
from app.services.i18n import available_ui
from app.services.locks import chat_lock
from app.services.scheduling import next_occurrence, validate_timezone


async def ensure_resolved(connection: Any, chat_id: int) -> None:
    """Do not abandon ambiguous or partly delivered payloads during a settings change."""
    if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE telegram_chat_id=$1 AND (status IN ('uncertain','sending') OR (status='failed' AND next_chunk>0)))",chat_id):
        raise UserError('pending_review')
    if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE telegram_chat_id=$1 AND status IN ('pending','retry') AND next_chunk>0)",chat_id):
        raise UserError('busy')


async def cancel_queued(connection: Any, chat_id: int) -> None:
    """Cancel only known-unsent/retry jobs; preserve the delivery history."""
    await connection.execute("""UPDATE delivery_log SET status='cancelled',updated_at=now(),
        error_code='configuration_changed' WHERE telegram_chat_id=$1 AND status IN ('pending','retry')""",chat_id)


async def configure_chat(connection: Any, chat_id: int, *, actor_id: int | None,
                         ui_language: str | None = None, translation_id: int | None = None,
                         timezone_name: str | None = None, message_thread_id: int | None = None,
                         set_thread: bool = False) -> Any:
    """Update one destination and its subscriptions, not other chats of that admin."""
    if ui_language is not None and ui_language not in available_ui():
        raise UserError('unknown_language')
    if timezone_name is not None:
        validate_timezone(timezone_name)
    if set_thread and message_thread_id is not None and message_thread_id < 1:
        raise UserError('invalid')
    async with chat_lock(connection,chat_id), connection.transaction():
        await ensure_resolved(connection,chat_id)
        old = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 FOR UPDATE',chat_id)
        if not old:
            raise UserError('no_result')
        translation = None
        if translation_id is not None:
            translation = await connection.fetchrow("""SELECT t.*,l.code AS language_code FROM translations t
                JOIN languages l ON l.id=t.language_id WHERE t.id=$1 AND t.is_active
                AND t.audit_status IN ('passed','passed_with_warnings')""",translation_id)
            if not translation:
                raise UserError('not_ready')
        await cancel_queued(connection,chat_id)
        changed_edition = translation_id is not None and translation_id != old['default_translation_id']
        row = await connection.fetchrow('''UPDATE telegram_chats SET
            ui_language=COALESCE($2,ui_language),default_translation_id=COALESCE($3,default_translation_id),
            bible_language_code=COALESCE($4,bible_language_code),timezone=COALESCE($5,timezone),
            message_thread_id=CASE WHEN $6 THEN $7 ELSE message_thread_id END,
            revision=revision+1,updated_at=now() WHERE telegram_chat_id=$1 RETURNING *''',
            chat_id,ui_language,translation_id,translation['language_code'] if translation else None,
            timezone_name,set_thread,message_thread_id)
        await connection.execute('''UPDATE subscriptions SET translation_id=COALESCE($2,translation_id),
            timezone=COALESCE($3,timezone),revision=revision+1,retry_at=NULL,
            current_book_code=CASE WHEN $4 THEN NULL ELSE current_book_code END,
            current_chapter=CASE WHEN $4 THEN NULL ELSE current_chapter END,
            current_verse=CASE WHEN $4 THEN NULL ELSE current_verse END,
            plan_day=CASE WHEN $4 THEN 0 ELSE plan_day END,
            completed=CASE WHEN $4 THEN false ELSE completed END,updated_at=now()
            WHERE telegram_chat_id=$1''',chat_id,translation_id,timezone_name,changed_edition)
        if changed_edition and translation:
            # A plan requiring the whole Bible must not silently run on a partial edition.
            await connection.execute('''UPDATE subscriptions SET is_enabled=false,next_run_at=NULL
                WHERE telegram_chat_id=$1 AND mode='reading_plan' AND
                ((plan_code LIKE 'bible-%' AND NOT $2) OR (plan_code='new-testament-90' AND NOT $3))''',
                chat_id,translation['canonical_66_complete'],translation['nt_complete'])
        schedules = await connection.fetch('SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND is_enabled',chat_id)
        for sub in schedules:
            next_run = (sub['next_run_at'] if timezone_name is None and sub['next_run_at'] is not None
                else next_occurrence(sub['send_time'],sub['timezone'],list(sub['days_of_week'])))
            await connection.execute('UPDATE subscriptions SET next_run_at=$2 WHERE id=$1',sub['id'],next_run)
        await connection.execute('''INSERT INTO operator_events(actor_id,chat_id,action,details)
            VALUES($1,$2,'chat_settings',$3::jsonb)''',actor_id,chat_id,json.dumps({
                'ui_language':row['ui_language'],'translation_id':row['default_translation_id'],
                'timezone':row['timezone'],'revision':row['revision'],'edition_progress_reset':changed_edition}))
    return row
