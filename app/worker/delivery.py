"""Durable SQL outbox: frozen payload, per-message acknowledgement, final progress."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime,timezone,timedelta
from typing import Any
from zoneinfo import ZoneInfo
from app.services import bible
from app.services import illustrations,devotionals,readings
from app.services.errors import UserError
from app.services.i18n import tr
from app.services.locks import chat_lock
from app.services.outbox import Envelope,dispatch_chunk
from app.services.plans import PLANS,plan_window,validate_plan
from app.services.scheduling import next_reading


def decoded(value: Any) -> Any:
    """asyncpg returns JSON as text unless a codec is configured."""
    return json.loads(value) if isinstance(value,str) else value


async def insert_payload(connection: Any, chat: Any, translation: Any, text: str,
                         key: str, mode: str, progress: dict[str,Any], *,
                         subscription: Any = None, max_length: int = 3900, image_id: int | None = None, frozen_chunks=None) -> int:
    """Persist content and references atomically before the first send request."""
    chunks = frozen_chunks if frozen_chunks is not None else illustrations.chunks(text,image_id,max_length)
    if frozen_chunks is None:
        from app.services.message_languages import prepare
        chunks = await prepare(connection,text,chunks,chat,source_key='outbox:'+key)
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


async def prepare_subscription(connection: Any, subscription_id: int, max_length: int = 3900, *, lead_seconds: int = 0) -> int | None:
    """Freeze one due occurrence, using its original local date, not a retry date."""
    row = await connection.fetchrow('SELECT telegram_chat_id FROM subscriptions WHERE id=$1',subscription_id)
    if not row:
        return None
    async with chat_lock(connection,row['telegram_chat_id']),connection.transaction():
        sub = await connection.fetchrow('SELECT * FROM subscriptions WHERE id=$1 FOR UPDATE',subscription_id)
        if not sub or not sub['is_enabled'] or sub['completed'] or not sub['next_run_at'] or sub['next_run_at']>datetime.now(timezone.utc)+timedelta(seconds=lead_seconds):
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
        image_id = None
        if mode in {'verse_of_day','topic_of_day','morning_verse','evening_verse'}:
            row = await readings.daily(connection,edition,str(chat['telegram_chat_id']),local_date) if mode=='verse_of_day' else None
            if mode=='topic_of_day':
                result = await bible.topic_verse(connection,edition,sub['topic_code'],str(chat['telegram_chat_id']),local_date)
                row = await readings.contextual(connection,edition,result[1]) if result else None
            if mode in devotionals.SLOTS:
                row,_ = await devotionals.selected_verse(connection,edition,chat['telegram_chat_id'],local_date,mode)
            if not row:
                raise UserError('no_result')
            from app.services.message_languages import with_title
            text = with_title(await bible.render_verse(connection,row,edition,ui_language=locale),mode,locale)
            text = await illustrations.decorate(connection,text,row,edition,chat=chat,
                request_key=f"subscription:{sub['id']}:{sub['revision']}:{sub['next_run_at'].isoformat()}",
                thread_id=chat['message_thread_id'])
            image_id = getattr(text,'image_id',None)
        elif mode=='sequential':
            reference = await bible.next_chapter_reference(connection,edition['id'],sub['current_book_code'],sub['current_chapter'])
            if reference:
                text = await bible.render_chapter(connection,edition,*reference,ui_language=locale)
                if text:
                    text = await illustrations.decorate_chapter(connection,text,edition,*reference,chat=chat,
                        request_key=f"chapter-subscription:{sub['id']}:{sub['revision']}:{sub['next_run_at'].isoformat()}",
                        thread_id=chat['message_thread_id'],max_length=max_length)
                progress.update(book=reference[0],chapter=reference[1],completed=not bool(
                    await bible.next_chapter_reference(connection,edition['id'],*reference)))
            else:
                text = tr(locale,'complete')
                progress['completed'] = True
        elif mode=='reading_plan':
            await validate_plan(connection,edition,sub['plan_code'])
            duration,scope = PLANS[sub['plan_code']]
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
            from app.services.message_languages import combine
            text = combine(pieces,footer='\n\n'+bible.attribution(edition,locale)) if pieces else tr(locale,'complete')
            progress.update(plan_day=min(sub['plan_day']+1,duration),completed=end==len(chapters))
            if pieces:
                progress.update(book=chapters[end-1]['book_code'],chapter=chapters[end-1]['chapter'])
        else:
            raise UserError('invalid')
        if not text:
            raise UserError('no_result')
        key = hashlib.sha256(f"{sub['id']}:{sub['revision']}:{sub['next_run_at'].isoformat()}".encode()).hexdigest()
        identifier=await insert_payload(connection,chat,edition,text,key,mode,progress,subscription=sub,max_length=max_length,image_id=image_id)
        if mode in {'verse_of_day','topic_of_day','morning_verse','evening_verse'}:
            from app.services import scheduled_media
            await scheduled_media.prepare(connection,identifier,row,edition,sub,chat,local_date)
        return identifier


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
        text = await illustrations.decorate_chapter(connection,text,translation,*reference,chat=chat,
            request_key=key,thread_id=chat['message_thread_id'],max_length=max_length)
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
    elif delivery['subscription_id'] and progress.get('kind')=='subscription':
        sub = await connection.fetchrow('SELECT * FROM subscriptions WHERE id=$1 FOR UPDATE',delivery['subscription_id'])
        if not sub or sub['revision']!=delivery['subscription_revision']:
            raise RuntimeError('Subscription changed during an acknowledged send')
        completed = progress.get('completed',False)
        next_run = None if completed else next_reading(sub['mode'],sub['send_time'],sub['timezone'],list(sub['days_of_week']))
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
            chunk = envelope.chunks[envelope.next_chunk]
            if isinstance(chunk,dict) and chunk.get('kind')=='rich':
                await illustrations.bind_message(self.connection,envelope.chat_id,chunk.get('request_id'),message_id)
                if chunk.get('card_id'):
                    from app.services.message_languages import bind
                    await bind(self.connection,chunk['card_id'],envelope.chat_id,message_id)
            if final and delivery['mode']=='illustration_edit':
                await self.connection.execute("UPDATE illustration_requests SET state='delivered' WHERE delivery_id=$1 AND telegram_message_id=$2",envelope.id,message_id)

    async def fail(self, envelope: Envelope, kind: str, retry_after: float) -> None:
        """Known rejection is retryable; ambiguity blocks automation until reviewed."""
        async with self.connection.transaction():
            row = await self.connection.fetchrow('SELECT * FROM delivery_log WHERE id=$1 FOR UPDATE',envelope.id)
            failures = row['consecutive_failures']+1
            editing = row['mode']=='illustration_edit'
            status = 'retry' if (editing and kind in {'retry','forbidden','uncertain'}) or kind=='retry' and failures<=10 else 'uncertain' if kind=='uncertain' else 'failed'
            if editing and status=='retry':
                retry_after=max(retry_after,min(3600,3*2**min(failures,11)))
            retry_at = datetime.now(timezone.utc)+timedelta(seconds=max(retry_after,3)) if status=='retry' else None
            await self.connection.execute('''UPDATE delivery_log SET status=$2,error_code=$3,error_message=$3,
                retry_at=$4,consecutive_failures=$5,sending_chunk=CASE WHEN $2='uncertain' THEN sending_chunk ELSE NULL END,
                updated_at=now() WHERE id=$1''',envelope.id,status,kind,retry_at,failures)
            if status in {'uncertain','failed'} and row['subscription_id']:
                # Do not increment revision: a reviewed acknowledgement still belongs to this version.
                await self.connection.execute('UPDATE subscriptions SET is_enabled=false,next_run_at=NULL WHERE id=$1',row['subscription_id'])
            if kind=='forbidden':
                await self.connection.execute('UPDATE telegram_chats SET is_active=false WHERE telegram_chat_id=$1',envelope.chat_id)
            if editing and status=='failed':
                await self.connection.execute("UPDATE illustration_requests SET state='unavailable' WHERE delivery_id=$1",envelope.id)


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
        editing = row['mode']=='illustration_edit'
        if editing:
            request = await connection.fetchrow('SELECT * FROM illustration_requests WHERE delivery_id=$1',delivery_id)
            if request and not await illustrations.valid_request_source(connection,request):
                async with connection.transaction():
                    await connection.execute("UPDATE illustration_requests SET state='cancelled' WHERE id=$1",request['id'])
                    await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='source_changed',updated_at=now() WHERE id=$1",delivery_id)
                return 'source_changed'
        valid = editing or row['chat_revision']==row['current_chat_revision'] and (
            not row['subscription_id'] or row['subscription_revision']==row['current_subscription_revision'])
        if not valid:
            await connection.execute("UPDATE delivery_log SET status='cancelled',error_code='stale_configuration',updated_at=now() WHERE id=$1",delivery_id)
            return 'stale'
        if row['scheduled_for']>datetime.now(timezone.utc):
            return 'not_due'
        from app.services import scheduled_media, prayers
        if row['mode']=='prayer' and datetime.now(timezone.utc)>row['scheduled_for']+timedelta(minutes=3):
            await connection.execute("UPDATE delivery_log SET status='skipped',error_code='prayer_expired',updated_at=now() WHERE id=$1",delivery_id)
            return 'expired'
        readiness=await scheduled_media.ready(connection,row)
        if readiness!='ready':
            return readiness
        if row['mode']=='prayer' and row['next_chunk']==0 and row['attempt_count']==0:
            parts=await prayers.artwork_for_send(connection,row)
            await connection.execute('UPDATE delivery_log SET chunks=$2::jsonb WHERE id=$1',delivery_id,json.dumps(parts))
            row=dict(row,chunks=parts)
        return await dispatch_chunk(Envelope(row['id'],row['telegram_chat_id'],tuple(decoded(row['chunks'])),
            row['next_chunk'],row['message_thread_id']),SqlCheckpoints(connection),sender)


async def recover_ambiguous(connection: Any) -> int:
    """Only run after acquiring the singleton-worker lock, never using an arbitrary lease."""
    async with connection.transaction():
        await connection.execute("""UPDATE delivery_log SET status='retry',sending_chunk=NULL,
            retry_at=now(),error_code='edit_interrupted',updated_at=now()
            WHERE status='sending' AND progress->>'kind'='illustration_edit'""")
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
