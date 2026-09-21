"""Durable SQL outbox: frozen payload, per-message acknowledgement, final progress."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime,timezone,timedelta
from typing import Any
from zoneinfo import ZoneInfo
from app.services import bible
from app.services.errors import UserError
from app.services.formatting import split_message
from app.services.i18n import tr
from app.services.locks import chat_lock
from app.services.outbox import Envelope,dispatch_chunk
from app.services.plans import PLANS,plan_window
from app.services.scheduling import next_occurrence


def decoded(value: Any) -> Any:
    """asyncpg returns JSON as text unless a codec is configured."""
    return json.loads(value) if isinstance(value,str) else value


async def insert_payload(connection: Any, chat: Any, translation: Any, text: str,
                         key: str, mode: str, progress: dict[str,Any], *,
                         subscription: Any = None, max_length: int = 3900) -> int:
    """Persist content and references atomically before the first send request."""
    chunks = split_message(text,max_length)
    if not chunks:
        raise ValueError('An empty payload cannot enter the delivery queue')
    identifier = await connection.fetchval('''INSERT INTO delivery_log(
        subscription_id,telegram_chat_id,translation_id,mode,payload_key,scheduled_for,
        status,payload_preview,chunks,progress,subscription_revision,chat_revision,ui_language,message_thread_id)
        VALUES($1,$2,$3,$4,$5,$6,'pending',$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13)
        ON CONFLICT(telegram_chat_id,payload_key) DO NOTHING RETURNING id''',
        subscription['id'] if subscription else None,chat['telegram_chat_id'],translation['id'],mode,key,
        subscription['next_run_at'] if subscription else datetime.now(timezone.utc),text[:1000],
        json.dumps(chunks,ensure_ascii=False),json.dumps(progress),subscription['revision'] if subscription else None,
        chat['revision'],chat['ui_language'],chat['message_thread_id'])
    if identifier is None:
        identifier = await connection.fetchval('SELECT id FROM delivery_log WHERE telegram_chat_id=$1 AND payload_key=$2',chat['telegram_chat_id'],key)
    return identifier


async def prepare_subscription(connection: Any, subscription_id: int, max_length: int = 3900) -> int | None:
    """Freeze one due occurrence, using its original local date, not a retry date."""
    row = await connection.fetchrow('SELECT telegram_chat_id FROM subscriptions WHERE id=$1',subscription_id)
    if not row:
        return None
    async with chat_lock(connection,row['telegram_chat_id']),connection.transaction():
        sub = await connection.fetchrow('SELECT * FROM subscriptions WHERE id=$1 FOR UPDATE',subscription_id)
        if not sub or not sub['is_enabled'] or sub['completed'] or not sub['next_run_at'] or sub['next_run_at']>datetime.now(timezone.utc):
            return None
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',sub['telegram_chat_id'])
        if not chat or not chat['is_active']:
            return None
        if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE subscription_id=$1 AND status IN ('pending','retry','sending','uncertain'))",subscription_id):
            return None
        edition = await bible.find_translation(connection,sub['translation_id'])
        if not edition:
            raise UserError('not_ready')
        locale = chat['ui_language']
        local_date = sub['next_run_at'].astimezone(ZoneInfo(sub['timezone'])).date()
        progress: dict[str,Any] = {'kind':'subscription','completed':False}
        mode = sub['mode']
        if mode in {'verse_of_day','topic_of_day'}:
            row = await bible.verse_of_day(connection,edition,str(chat['telegram_chat_id']),local_date) if mode=='verse_of_day' else None
            if mode=='topic_of_day':
                result = await bible.topic_verse(connection,edition,sub['topic_code'],str(chat['telegram_chat_id']),local_date)
                row = result[1] if result else None
            if not row:
                raise UserError('no_result')
            text = f"<b>{tr(locale,mode)}</b>\n\n"+await bible.render_verse(connection,row,edition,ui_language=locale)
        elif mode=='sequential':
            reference = await bible.next_chapter_reference(connection,edition['id'],sub['current_book_code'],sub['current_chapter'])
            if reference:
                text = await bible.render_chapter(connection,edition,*reference,ui_language=locale)
                progress.update(book=reference[0],chapter=reference[1],completed=not bool(
                    await bible.next_chapter_reference(connection,edition['id'],*reference)))
            else:
                text = tr(locale,'complete')
                progress['completed'] = True
        elif mode=='reading_plan':
            duration,scope = PLANS[sub['plan_code']]
            if scope is None and not edition['canonical_66_complete'] or scope=='NT' and not edition['nt_complete']:
                raise UserError('invalid')
            chapters = await connection.fetch('''SELECT c.book_code,c.chapter FROM translation_chapters c
                JOIN books b ON b.code=c.book_code WHERE c.translation_id=$1
                AND ($2::text IS NULL OR b.testament=$2 OR c.book_code=$2) ORDER BY c.position''',edition['id'],scope)
            start,end = plan_window(len(chapters),duration,sub['plan_day'])
            pieces = []
            for ref in chapters[start:end]:
                part = await bible.render_chapter(connection,edition,ref['book_code'],ref['chapter'],ui_language=locale,with_attribution=False)
                if not part:
                    raise ValueError('Missing chapter while composing a reading plan')
                pieces.append(part)
            text = ('\n\n'.join(pieces)+'\n\n'+bible.attribution(edition,locale)) if pieces else tr(locale,'complete')
            progress.update(plan_day=min(sub['plan_day']+1,duration),completed=end==len(chapters))
            if pieces:
                progress.update(book=chapters[end-1]['book_code'],chapter=chapters[end-1]['chapter'])
        else:
            raise UserError('invalid')
        if not text:
            raise UserError('no_result')
        key = hashlib.sha256(f"{sub['id']}:{sub['revision']}:{sub['next_run_at'].isoformat()}".encode()).hexdigest()
        return await insert_payload(connection,chat,edition,text,key,mode,progress,subscription=sub,max_length=max_length)


async def enqueue_next(connection: Any, chat: Any, translation: Any, request_id: str,
                       max_length: int = 3900) -> int:
    """Manual reading is per destination and advances only after confirmed delivery."""
    chat_id = chat['telegram_chat_id']
    async with chat_lock(connection,chat_id),connection.transaction():
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',chat_id)
        # Check settings again after acquiring the lock, avoiding a translation-switch race.
        if chat['default_translation_id'] is None:
            chosen = await bible.chat_translation(connection,chat_id)
            if not chosen or chosen['id']!=translation['id']:
                raise UserError('invalid')
            await connection.execute('UPDATE telegram_chats SET default_translation_id=$2,bible_language_code=$3 WHERE telegram_chat_id=$1',chat_id,translation['id'],translation['language_code'])
        elif chat['default_translation_id'] != translation['id']:
            raise UserError('invalid')
        key = 'manual:'+hashlib.sha256(f'{chat_id}:{request_id}'.encode()).hexdigest()
        existing = await connection.fetchval('SELECT id FROM delivery_log WHERE telegram_chat_id=$1 AND payload_key=$2',chat_id,key)
        if existing:
            return existing
        if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM delivery_log WHERE telegram_chat_id=$1 AND subscription_id IS NULL AND mode='manual' AND status IN ('pending','sending','retry','uncertain'))",chat_id):
            raise UserError('busy')
        saved = await connection.fetchrow('SELECT * FROM chat_reading_progress WHERE telegram_chat_id=$1 AND translation_id=$2',chat_id,translation['id'])
        reference = await bible.next_chapter_reference(connection,translation['id'],saved['book_code'] if saved else None,saved['chapter'] if saved else None)
        if not reference:
            raise UserError('complete')
        text = await bible.render_chapter(connection,translation,*reference,ui_language=chat['ui_language'])
        if not text:
            raise UserError('no_result')
        return await insert_payload(connection,chat,translation,text,key,'manual',
            {'kind':'manual','book':reference[0],'chapter':reference[1]},max_length=max_length)


async def commit_progress(connection: Any, delivery: Any) -> None:
    """Called inside the acknowledgement transaction; no progress before all chunks."""
    progress = decoded(delivery['progress'])
    if progress.get('kind')=='manual' and progress.get('book'):
        await connection.execute('''INSERT INTO chat_reading_progress(telegram_chat_id,translation_id,book_code,chapter)
            VALUES($1,$2,$3,$4) ON CONFLICT(telegram_chat_id,translation_id) DO UPDATE SET
            book_code=EXCLUDED.book_code,chapter=EXCLUDED.chapter,updated_at=now()''',delivery['telegram_chat_id'],
            delivery['translation_id'],progress['book'],progress['chapter'])
    elif delivery['subscription_id']:
        sub = await connection.fetchrow('SELECT * FROM subscriptions WHERE id=$1 FOR UPDATE',delivery['subscription_id'])
        if not sub or sub['revision']!=delivery['subscription_revision']:
            raise RuntimeError('Subscription changed during an acknowledged send')
        completed = progress.get('completed',False)
        next_run = None if completed else next_occurrence(sub['send_time'],sub['timezone'],list(sub['days_of_week']))
        await connection.execute('''UPDATE subscriptions SET current_book_code=COALESCE($2,current_book_code),
            current_chapter=COALESCE($3,current_chapter),plan_day=COALESCE($4,plan_day),completed=$5,
            is_enabled=CASE WHEN $5 THEN false ELSE is_enabled END,next_run_at=$6,last_run_at=now(),
            retry_at=NULL,locked_at=NULL,lock_token=NULL,updated_at=now() WHERE id=$1''',sub['id'],
            progress.get('book'),progress.get('chapter'),progress.get('plan_day'),completed,next_run)


class SqlCheckpoints:
    """SQL adapter used while holding the destination advisory lock."""
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def begin(self, envelope: Envelope) -> bool:
        """Mark ambiguity before starting network I/O, never afterwards."""
        result = await self.connection.execute('''UPDATE delivery_log SET status='sending',sending_chunk=next_chunk,
            attempt_count=attempt_count+1,updated_at=now() WHERE id=$1 AND next_chunk=$2 AND status IN ('pending','retry')''',
            envelope.id,envelope.next_chunk)
        return result=='UPDATE 1'

    async def acknowledge(self, envelope: Envelope, message_id: int) -> None:
        """Commit message ID, chunk offset, final state and reading progress together."""
        async with self.connection.transaction():
            delivery = await self.connection.fetchrow('SELECT * FROM delivery_log WHERE id=$1 FOR UPDATE',envelope.id)
            if delivery['status']!='sending' or delivery['sending_chunk']!=envelope.next_chunk:
                raise RuntimeError('Acknowledgement checkpoint conflict')
            final = envelope.next_chunk+1==len(envelope.chunks)
            await self.connection.execute('''UPDATE delivery_log SET status=$2,next_chunk=next_chunk+1,
                sending_chunk=NULL,telegram_message_ids=array_append(telegram_message_ids,$3),
                consecutive_failures=0,retry_at=NULL,error_code=NULL,error_message=NULL,
                sent_at=CASE WHEN $4 THEN now() ELSE sent_at END,updated_at=now() WHERE id=$1''',
                envelope.id,'sent' if final else 'pending',message_id,final)
            if final:
                await commit_progress(self.connection,delivery)

    async def fail(self, envelope: Envelope, kind: str, retry_after: float) -> None:
        """Known rejection is retryable; ambiguity blocks automation until reviewed."""
        async with self.connection.transaction():
            row = await self.connection.fetchrow('SELECT * FROM delivery_log WHERE id=$1 FOR UPDATE',envelope.id)
            failures = row['consecutive_failures']+1
            status = 'retry' if kind=='retry' and failures<=10 else 'uncertain' if kind=='uncertain' else 'failed'
            retry_at = datetime.now(timezone.utc)+timedelta(seconds=max(retry_after,3)) if status=='retry' else None
            await self.connection.execute('''UPDATE delivery_log SET status=$2,error_code=$3,error_message=$3,
                retry_at=$4,consecutive_failures=$5,sending_chunk=CASE WHEN $2='uncertain' THEN sending_chunk ELSE NULL END,
                updated_at=now() WHERE id=$1''',envelope.id,status,kind,retry_at,failures)
            if status in {'uncertain','failed'} and row['subscription_id']:
                # Do not increment revision: a reviewed acknowledgement still belongs to this version.
                await self.connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE id=$1',row['subscription_id'])
            if kind=='forbidden':
                await self.connection.execute('UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1',envelope.chat_id)


async def process_delivery(connection: Any, delivery_id: int, sender: Any) -> str:
    """Cancel stale snapshots rather than sending old-language content after a change."""
    hint = await connection.fetchrow('SELECT telegram_chat_id FROM delivery_log WHERE id=$1',delivery_id)
    if not hint:
        return 'absent'
    async with chat_lock(connection,hint['telegram_chat_id']):
        row = await connection.fetchrow('''SELECT d.*,c.revision AS current_chat_revision,c.is_active,
            s.revision AS current_subscription_revision,s.is_enabled FROM delivery_log d
            JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id LEFT JOIN subscriptions s ON s.id=d.subscription_id
            WHERE d.id=$1''',delivery_id)
        if row['status'] not in {'pending','retry'} or row['retry_at'] and row['retry_at']>datetime.now(timezone.utc):
            return 'not_due'
        if not row['is_active'] or row['subscription_id'] and not row['is_enabled']:
            return 'paused'
        valid = row['chat_revision']==row['current_chat_revision'] and (
            not row['subscription_id'] or row['subscription_revision']==row['current_subscription_revision'])
        if not valid:
            await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='stale_configuration',updated_at=now() WHERE id=$1",delivery_id)
            return 'stale'
        return await dispatch_chunk(Envelope(row['id'],row['telegram_chat_id'],tuple(decoded(row['chunks'])),
            row['next_chunk'],row['message_thread_id']),SqlCheckpoints(connection),sender)


async def recover_ambiguous(connection: Any) -> int:
    """Only run after acquiring the singleton-worker lock, never using an arbitrary lease."""
    async with connection.transaction():
        result = await connection.execute("UPDATE delivery_log SET status='uncertain',error_code='worker_interrupted',updated_at=now() WHERE status='sending'")
        await connection.execute("UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE id IN (SELECT subscription_id FROM delivery_log WHERE status='uncertain')")
    return int(result.split()[-1])


async def resolve_uncertain(connection: Any, delivery_id: int, chat_id: int, actor_id: int, action: str) -> None:
    """Explicit human decision: sent, retry-duplicate-risk, or cancel; caller verifies chat admin."""
    if action not in {'sent','retry-duplicate-risk','cancel'}:
        raise UserError('invalid')
    async with chat_lock(connection,chat_id),connection.transaction():
        row = await connection.fetchrow("SELECT * FROM delivery_log WHERE id=$1 AND telegram_chat_id=$2 AND status IN ('uncertain','failed') FOR UPDATE",delivery_id,chat_id)
        if not row:
            raise UserError('no_result')
        chunks = decoded(row['chunks'])
        if not chunks and action!='cancel':
            raise UserError('invalid')
        if action=='sent':
            # A human-observed acknowledgement has no API message id: preserve an audit event instead.
            final = row['next_chunk']+1==len(chunks)
            await connection.execute("UPDATE delivery_log SET status=$2,next_chunk=next_chunk+1,sending_chunk=NULL,error_code='operator_confirmed',updated_at=now(),sent_at=CASE WHEN $2='sent' THEN now() ELSE sent_at END WHERE id=$1",delivery_id,'sent' if final else 'pending')
            if final:
                await commit_progress(connection,row)
        else:
            await connection.execute("UPDATE delivery_log SET status=$2,sending_chunk=NULL,retry_at=NULL,error_code='operator_resolved',updated_at=now() WHERE id=$1",delivery_id,'pending' if action=='retry-duplicate-risk' else 'cancelled')
        if row['subscription_id'] and action!='cancel':
            await connection.execute('UPDATE subscriptions SET is_enabled=NOT completed WHERE id=$1',row['subscription_id'])
        await connection.execute("INSERT INTO operator_events(actor_id,chat_id,action,details) VALUES($1,$2,'resolve_delivery',$3::jsonb)",actor_id,chat_id,json.dumps({'delivery_id':delivery_id,'action':action,'chunk':row['next_chunk']}))
