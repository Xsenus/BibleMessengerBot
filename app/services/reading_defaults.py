"""Стандартные чтения и добровольное возвращение после ручной паузы."""
from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.services import bible
from app.services.errors import UserError
from app.services.formatting import split_message
from app.services.locks import chat_lock
from app.services.scheduling import next_occurrence, next_reading, on_date, parse_hhmm

DEFAULT_TIMES = {'morning_verse': time(9, 38), 'verse_of_day': time(14), 'evening_verse': time(21, 13)}
OFFER_TIME = time(20)
OFFER_WINDOW = timedelta(minutes=10)
ELIGIBLE = """c.is_active AND c.chat_type='private' AND c.platform IN ('telegram','max')
    AND NOT EXISTS (SELECT 1 FROM telegram_users u WHERE u.telegram_user_id=c.registered_by AND u.is_blocked)"""


def supported(chat: Any) -> bool:
    """Стандарт по умолчанию применяется к личным чатам Telegram и MAX."""
    return chat.get('chat_type') == 'private' and chat.get('platform', 'telegram') in {'telegram', 'max'}


def default_time(chat: Any, mode: str, fallback: str) -> time:
    """Выбрать стандартный час в личном чате, сохранив настройку для прочих режимов и чатов."""
    if supported(chat) and mode in DEFAULT_TIMES:
        return DEFAULT_TIMES[mode]
    return parse_hhmm(fallback)


async def _preferences(connection: Any, chat_id: int) -> Any:
    """Подготовить постоянную запись выбора пользователя под блокировкой чата."""
    await connection.execute('''INSERT INTO reading_schedule_preferences(telegram_chat_id)
        VALUES($1) ON CONFLICT DO NOTHING''', chat_id)
    return await connection.fetchrow('SELECT * FROM reading_schedule_preferences WHERE telegram_chat_id=$1', chat_id)


async def remember_choice(connection: Any, chat_id: int, mode: str | None, choice: str) -> None:
    """Записать паузу, включение или отписку. Вызывать под блокировкой чата в транзакции."""
    if mode is not None and mode not in DEFAULT_TIMES:
        return
    chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', chat_id)
    if not chat or not supported(chat):
        return
    prefs = await _preferences(connection, chat_id)
    paused, unsubscribed = set(prefs['paused_modes']), set(prefs['unsubscribed_modes'])
    selected = {mode} if mode else set(DEFAULT_TIMES)
    offer_at = prefs['offer_at']
    if choice == 'pause':
        newly_paused = selected - paused - unsubscribed
        if not newly_paused:
            return
        paused.update(newly_paused)
        if not prefs['offers_muted']:
            offer_at = offer_at or next_occurrence(OFFER_TIME, chat['timezone'])
    elif choice == 'unsubscribe':
        paused.difference_update(selected)
        unsubscribed.update(selected)
    else:
        if not selected.intersection(paused | unsubscribed):
            return
        paused.difference_update(selected)
        unsubscribed.difference_update(selected)

    # Сначала сохраняем новое решение, затем отменяем только неотправленное приглашение.
    invalidate = choice == 'pause' or not paused
    await connection.execute('''UPDATE reading_schedule_preferences
        SET paused_modes=$2,unsubscribed_modes=$3,offer_at=$4,
            offer_delivery_id=CASE WHEN $5 THEN NULL ELSE offer_delivery_id END
        WHERE telegram_chat_id=$1''', chat_id, sorted(paused), sorted(unsubscribed), offer_at if paused else None, invalidate)
    if invalidate and prefs['offer_delivery_id']:
        await connection.execute('''UPDATE delivery_log SET status='cancelled',error_code='reading_preferences_changed',updated_at=now()
            WHERE id=$1 AND status IN ('pending','retry')''', prefs['offer_delivery_id'])


async def _avoid_repeat(connection: Any, subscription: Any, clock: time, now: datetime) -> datetime:
    """При смене времени не назначать второй выпуск того же режима на ту же местную дату."""
    zone = subscription['timezone']
    days = list(subscription['days_of_week'])
    mode = subscription['mode']
    scheduled = next_reading(mode, clock, zone, days, now=now)
    day = scheduled.astimezone(ZoneInfo(zone)).date()
    start = on_date(day, time(), zone)
    end = on_date(day + timedelta(days=1), time(), zone)
    acknowledged = await connection.fetchval('''SELECT EXISTS(SELECT 1 FROM delivery_log
        WHERE telegram_chat_id=$1 AND mode=$2 AND status='sent' AND scheduled_for>=$3 AND scheduled_for<$4)''',
        subscription['telegram_chat_id'], mode, start, end)
    return next_reading(mode, clock, zone, days, now=scheduled) if acknowledged else scheduled


async def initialize(connection: Any, chat_id: int, *, now: datetime | None = None) -> None:
    """Заполнить стандарт в новом или существующем чате, сохранив отключения и пользовательское время."""
    from app.services.subscriptions import REVIEW_PENDING, cancel_subscription_jobs, create_or_update_subscription

    current = now or datetime.now(UTC)
    async with chat_lock(connection, chat_id), connection.transaction():
        chat = await connection.fetchrow(f'SELECT c.* FROM telegram_chats c WHERE c.telegram_chat_id=$1 AND {ELIGIBLE}', chat_id)
        if not chat:
            return
        prefs = await _preferences(connection, chat_id)
        if prefs['initialized'] or await connection.fetchval(REVIEW_PENDING, chat_id):
            return
        edition = await bible.chat_translation(connection, chat_id)
        if not edition:
            return
        existing = {r['mode']: r for r in await connection.fetch(
            'SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND mode=ANY($2::text[])', chat_id, list(DEFAULT_TIMES))}
        unsubscribed = set(prefs['unsubscribed_modes'])
        history = await connection.fetch('''SELECT DISTINCT mode FROM delivery_log
            WHERE telegram_chat_id=$1 AND error_code='unsubscribed' AND mode=ANY($2::text[])''',
            chat_id, list(DEFAULT_TIMES))
        unsubscribed.update(r['mode'] for r in history if r['mode'] not in existing)
        paused = set(prefs['paused_modes'])
        failed_modes = {r['mode'] for r in await connection.fetch('''SELECT DISTINCT mode FROM delivery_log
            WHERE telegram_chat_id=$1 AND status IN ('failed','uncertain') AND mode=ANY($2::text[])''',
            chat_id, list(DEFAULT_TIMES))}
        paused.update(mode for mode, sub in existing.items()
                      if not sub['is_enabled'] and not sub['paused_reason'] and mode not in unsubscribed
                      and mode not in failed_modes)
        for mode, clock in DEFAULT_TIMES.items():
            sub = existing.get(mode)
            if not sub and mode not in paused | unsubscribed:
                sub = await create_or_update_subscription(
                    connection, chat_id=chat_id, created_by=chat['registered_by'], translation_id=edition['id'],
                    mode=mode, send_time=clock, timezone_name=chat['timezone'])
                due = await _avoid_repeat(connection, sub, clock, current)
                await connection.execute('UPDATE subscriptions SET next_run_at=$2 WHERE id=$1', sub['id'], due)
            elif sub and mode == 'verse_of_day' and sub['send_time'] == time(9):
                migrated_due = await _avoid_repeat(connection, sub, clock, current) if sub['is_enabled'] else None
                await cancel_subscription_jobs(connection, sub['id'])
                await connection.execute('''UPDATE subscriptions SET send_time=$2,next_run_at=$3,
                    revision=revision+1,updated_at=now() WHERE id=$1''', sub['id'], clock, migrated_due)
        offer_at: datetime | None = prefs['offer_at']
        if paused and not prefs['offers_muted'] and not prefs['offer_delivery_id']:
            offer_at = offer_at or next_occurrence(OFFER_TIME, chat['timezone'], now=current)
        await connection.execute('''UPDATE reading_schedule_preferences SET initialized=true,
            paused_modes=$2,unsubscribed_modes=$3,offer_at=$4 WHERE telegram_chat_id=$1''',
            chat_id, sorted(paused), sorted(unsubscribed), offer_at)


async def _prepare_offer(connection: Any, chat_id: int, now: datetime) -> None:
    """Поставить одно приглашение в штатную очередь с кнопками, привязанными к ID доставки."""
    from app.bot.common import button
    from app.maxbot.ui import keyboard_attachment
    from app.services.subscriptions import REVIEW_PENDING
    from app.worker.delivery import insert_payload

    async with chat_lock(connection, chat_id), connection.transaction():
        chat = await connection.fetchrow(f'SELECT c.* FROM telegram_chats c WHERE c.telegram_chat_id=$1 AND {ELIGIBLE}', chat_id)
        if not chat:
            return
        prefs = await _preferences(connection, chat_id)
        slot = prefs['offer_at']
        if not slot or slot > now or prefs['offers_muted'] or not prefs['paused_modes']:
            return
        if now >= slot + OFFER_WINDOW:
            await connection.execute('UPDATE reading_schedule_preferences SET offer_at=$2 WHERE telegram_chat_id=$1',
                                     chat_id, next_occurrence(OFFER_TIME, chat['timezone'], now=now))
            return
        if await connection.fetchval(REVIEW_PENDING, chat_id):
            return
        day = slot.astimezone(ZoneInfo(chat['timezone'])).date()
        edition = await bible.chat_translation(connection, chat_id)
        row = await bible.verse_of_day(connection, edition, f'offer:{chat_id}', day) if edition else None
        if not row:
            return
        locale = chat['ui_language']
        ru = locale == 'ru'
        verse = await bible.render_verse(connection, row, edition, ui_language=locale)
        intro = ('Вернуть приостановленные сообщения по расписанию?'
                 if ru else 'Turn your paused scheduled messages back on?')
        text = split_message(str(verse), 3000)[0] + '\n\n' + intro
        chunk: dict[str, Any] = dict(kind='max_text' if chat['platform'] == 'max' else 'reading_offer', text=text)
        identifier = await insert_payload(connection, chat, edition, text, f'reading-offer:{day}', 'reading_offer',
                                          {'kind': 'reading_offer'}, frozen_chunks=[chunk])
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [button('Включить рассылку' if ru else 'Enable readings', 'readingon', chat_id, str(identifier))],
            [button('Больше не предлагать' if ru else 'Do not remind me', 'readingno', chat_id, str(identifier))],
        ])
        if chat['platform'] == 'max':
            chunk['max_keyboard'] = keyboard_attachment(markup, locale=locale)
        else:
            chunk['inline_keyboard'] = markup.model_dump(mode='json')
        await connection.execute('''UPDATE delivery_log SET scheduled_for=$2,chunks=$3::jsonb
            WHERE id=$1 AND status='pending' AND attempt_count=0''', identifier, slot, json.dumps([chunk]))
        await connection.execute('''UPDATE reading_schedule_preferences SET offer_at=NULL,offer_delivery_id=$2
            WHERE telegram_chat_id=$1''', chat_id, identifier)


async def prepare_due(connection: Any, *, now: datetime | None = None) -> None:
    """Обновить небольшую порцию старых чатов и подготовить наступившие приглашения независимо от рассылок."""
    current = now or datetime.now(UTC)
    chats = await connection.fetch(f'''SELECT c.telegram_chat_id FROM telegram_chats c
        LEFT JOIN reading_schedule_preferences p USING(telegram_chat_id)
        WHERE {ELIGIBLE} AND NOT coalesce(p.initialized,false)
        AND NOT EXISTS(SELECT 1 FROM delivery_log d WHERE d.telegram_chat_id=c.telegram_chat_id
                       AND d.mode NOT IN ('illustration_edit','reading_offer')
                       AND (d.status IN ('sending','uncertain') OR
                           (d.status IN ('failed','pending','retry') AND d.next_chunk>0)))
        ORDER BY c.telegram_chat_id LIMIT 25''')
    for chat in chats:
        await initialize(connection, chat['telegram_chat_id'], now=current)
    due = await connection.fetch(f'''SELECT p.telegram_chat_id FROM reading_schedule_preferences p
        JOIN telegram_chats c USING(telegram_chat_id) WHERE {ELIGIBLE}
        AND p.offer_at<=$1 AND NOT p.offers_muted ORDER BY p.offer_at LIMIT 25''', current)
    for chat in due:
        await _prepare_offer(connection, chat['telegram_chat_id'], current)


async def valid_offer(connection: Any, delivery: Any) -> bool:
    """Перепроверить действующий выбор пользователя перед отправкой и отсечь поздние повторы."""
    if datetime.now(UTC) >= delivery['scheduled_for'] + OFFER_WINDOW:
        return False
    return bool(await connection.fetchval(f'''SELECT EXISTS(SELECT 1 FROM reading_schedule_preferences p
        JOIN telegram_chats c USING(telegram_chat_id) WHERE {ELIGIBLE} AND p.telegram_chat_id=$1
        AND p.offer_delivery_id=$2 AND NOT p.offers_muted AND cardinality(p.paused_modes)>0)''',
        delivery['telegram_chat_id'], delivery['id']))


async def answer_offer(connection: Any, chat: Any, actor_id: int, message_id: int, delivery_id: int, enable: bool) -> str:
    """Включить вручную приостановленные режимы или отключить будущие приглашения по кнопке подтверждённого сообщения."""
    from app.services.subscriptions import create_or_update_subscription, set_enabled

    chat_id = chat['telegram_chat_id']
    async with chat_lock(connection, chat_id), connection.transaction():
        chat = await connection.fetchrow(f'SELECT c.* FROM telegram_chats c WHERE c.telegram_chat_id=$1 AND {ELIGIBLE}', chat_id)
        prefs = await connection.fetchrow('SELECT * FROM reading_schedule_preferences WHERE telegram_chat_id=$1', chat_id)
        if not chat or not prefs or prefs['offers_muted'] or prefs['offer_delivery_id'] != delivery_id:
            raise UserError('invalid')
        ru = chat['ui_language'] == 'ru'
        matched = await connection.fetchval('''SELECT EXISTS(SELECT 1 FROM delivery_log WHERE id=$1 AND telegram_chat_id=$2
            AND mode='reading_offer' AND status='sent' AND $3=ANY(telegram_message_ids))''', delivery_id, chat_id, message_id)
        if not matched:
            raise UserError('invalid')
        if not enable:
            await connection.execute('''UPDATE reading_schedule_preferences
                SET offers_muted=true,offer_at=NULL,offer_delivery_id=NULL WHERE telegram_chat_id=$1''', chat_id)
            return 'Больше не буду предлагать включить рассылку.' if ru else 'I will not send more reminders to enable readings.'
        edition = await bible.chat_translation(connection, chat_id)
        if not edition:
            raise UserError('not_ready')
        current = datetime.now(UTC)
        for mode in prefs['paused_modes']:
            sub = await connection.fetchrow('SELECT * FROM subscriptions WHERE telegram_chat_id=$1 AND mode=$2', chat_id, mode)
            if sub:
                await set_enabled(connection, chat_id, True, mode)
            else:
                sub = await create_or_update_subscription(
                    connection, chat_id=chat_id, created_by=actor_id, translation_id=edition['id'], mode=mode,
                    send_time=DEFAULT_TIMES[mode], timezone_name=chat['timezone'])
            due = await _avoid_repeat(connection, sub, sub['send_time'], current)
            await connection.execute('UPDATE subscriptions SET next_run_at=$2 WHERE id=$1', sub['id'], due)
        return 'Рассылка снова включена. Ближайшие выпуски придут по расписанию.' if ru else 'Scheduled readings are back on.'
