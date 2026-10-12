"""Рассылка найденного отрывка всем получателям (по решению владельца бота).

Владелец ищет стих или отрывок, под результатом появляется предложение «Отправить всем».
После подтверждения отрывок ставится в общую очередь доставки для каждого активного
назначения (личные чаты, группы, каналы Telegram и MAX) на языке и в издании этого
назначения. Очередь, ограничитель скорости и контрольные точки — те же, что у обычных
сообщений, поэтому рассылка переживает перезапуск и не дублируется.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot.common import button
from app.services import bible, passage
from app.services.accounts import is_admin
from app.services.errors import UserError
from app.services.locks import chat_lock

LOGGER = logging.getLogger(__name__)
MAX_REFS = 5
# Предложение рассылки, подготовленное при обработке команды; читается и сбрасывается обработчиком.
_offer: ContextVar[int | None] = ContextVar('broadcast_offer', default=None)


def take_offer() -> int | None:
    """Забрать (и сбросить) номер черновика рассылки, подготовленный текущей командой."""
    value = _offer.get()
    _offer.set(None)
    return value


def refs_from_rows(rows: list[Any]) -> list[dict]:
    """Адреса отрывков по строкам стихов (результат поиска по словам): по одному на строку."""
    return [dict(book=r['book_code'], chapter=r['chapter'], first=r['verse'],
                 last=r.get('verse_end') or r['verse']) for r in rows]


def refs_from_reference(reference: Any, book: str) -> list[dict]:
    """Адрес отрывка по разобранному адресу (`Ин 3:16`, `Пс 22`); для главы целиком стихи не указываются."""
    return [dict(book=book, chapter=reference.chapter, first=reference.first, last=reference.last)]


async def offer(connection: Any, actor_id: int, refs: list[dict], translation_id: int | None,
                image_id: int | None = None) -> int | None:
    """Если автор команды — владелец бота, создать черновик рассылки и запомнить его номер.

    Не администратору и пустому результату ничего не предлагается. `image_id` — картинка, созданная (или заказанная)
    при поиске: она уйдёт всем получателям. Возвращает номер черновика или None.
    """
    if not refs or not await is_admin(connection, actor_id):
        return None
    draft = await connection.fetchval(
        'INSERT INTO broadcasts(created_by,refs,source_translation_id,image_id) VALUES($1,$2::jsonb,$3,$4) RETURNING id',
        actor_id, json.dumps(refs[:MAX_REFS]), translation_id, image_id)
    _offer.set(draft)
    return draft


def offer_markup(chat: Any, draft_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Сообщение с кнопкой «Отправить всем» под результатом поиска."""
    ru = chat['ui_language'] == 'ru'
    text = ('📣 Отправить этот отрывок всем пользователям?' if ru
            else '📣 Send this passage to all users?')
    label = '📣 Отправить всем' if ru else '📣 Send to all'
    return text, InlineKeyboardMarkup(inline_keyboard=[[button(label, 'bcast', chat['telegram_chat_id'], str(draft_id))]])


async def recipients(connection: Any) -> list[Any]:
    """Получатели рассылки отрывка — все активные назначения (см. fanout.recipients)."""
    from app.services import fanout
    return await fanout.recipients(connection)


async def load_draft(connection: Any, draft_id: int, actor_id: int) -> Any:
    """Черновик рассылки; чужой или несуществующий недоступен, действовать может только владелец."""
    if not await is_admin(connection, actor_id):
        raise UserError('forbidden')
    draft = await connection.fetchrow('SELECT * FROM broadcasts WHERE id=$1 AND created_by=$2', draft_id, actor_id)
    if not draft:
        raise UserError('no_result')
    return draft


async def confirmation(connection: Any, draft_id: int, actor_id: int, chat: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Запрос подтверждения: сколько получателей и кнопки «Подтвердить» / «Отмена»."""
    draft = await load_draft(connection, draft_id, actor_id)
    if draft['status'] != 'draft':
        raise UserError('invalid')
    rows = await recipients(connection)
    private = sum(1 for r in rows if r['chat_type'] == 'private')
    ru = chat['ui_language'] == 'ru'
    text = (f'Отправить отрывок всем? Получателей: {len(rows)} (личных: {private}, групп и каналов: {len(rows) - private}). '
            'Каждый получит его в своём издании и языке.' if ru else
            f'Send to everyone? Recipients: {len(rows)} (private: {private}, groups and channels: {len(rows) - private}). '
            'Each receives it in their own edition and language.')
    chat_id = chat['telegram_chat_id']
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        button('✅ Подтвердить' if ru else '✅ Confirm', 'bcastgo', chat_id, str(draft_id)),
        button('✖ Отмена' if ru else '✖ Cancel', 'bcastno', chat_id, str(draft_id))]])
    return text, markup


async def cancel(connection: Any, draft_id: int, actor_id: int) -> None:
    """Отменить неотправленный черновик."""
    await load_draft(connection, draft_id, actor_id)
    await connection.execute("UPDATE broadcasts SET status='cancelled',updated_at=now() WHERE id=$1 AND status='draft'", draft_id)


async def image_of(connection: Any, text: Any) -> int | None:
    """Картинка результата поиска: готовая или заказанная (ожидающая генерации); None, если её нет."""
    image = getattr(text, 'image_id', None)
    if image:
        return image
    request = getattr(text, 'request_id', None)
    if request:
        return await connection.fetchval('SELECT image_id FROM illustration_requests WHERE id=$1', request)
    return None


async def render_for(connection: Any, edition: Any, refs: list[dict], locale: str,
                     image_id: int | None = None) -> Any | None:
    """Текст рассылки в издании получателя; если любого из отрывков в издании нет — None."""
    from app.services.message_languages import combine
    pieces = []
    for item in refs:
        try:
            rows = await passage.fetch_rows(connection, edition, item['book'], item['chapter'], item['first'], item['last'])
        except UserError:
            return None
        reference = passage.Reference(item['book'], item['chapter'], item['first'], item['last'])
        pieces.append(await passage.render(connection, edition, (reference, item['book'], rows), locale))
    text = pieces[0] if len(pieces) == 1 else combine(pieces)
    if image_id is None:
        return text
    from app.services.illustrations import ReadingText
    from app.services.message_languages import inherit
    # Карточка чтения с общей картинкой: к ней же привяжутся кнопки языков и озвучка.
    return inherit(ReadingText(str(text), image_id, None), text)


async def send(connection: Any, draft_id: int, actor_id: int, max_length: int = 3900) -> dict[str, int]:
    """Поставить отрывок в очередь доставки всем получателям.

    Черновик атомарно переводится `draft → sending`: повторное нажатие ничего не дублирует.
    Для каждого назначения под блокировкой чата строится текст в его издании и создаётся
    задание с уникальным ключом `broadcast:<id>` (повторная постановка безопасна).
    Возвращает счётчики {recipients, queued, skipped}.
    """
    from app.worker.delivery import insert_payload
    await load_draft(connection, draft_id, actor_id)
    draft = await connection.fetchrow(
        "UPDATE broadcasts SET status='sending',updated_at=now() WHERE id=$1 AND status='draft' RETURNING *", draft_id)
    if not draft:
        raise UserError('invalid')
    refs = json.loads(draft['refs']) if isinstance(draft['refs'], str) else draft['refs']
    targets = await recipients(connection)
    queued = skipped = 0
    rendered: dict[tuple[int, str], Any] = {}        # (издание, язык интерфейса) → готовый текст: один раз на все чаты с тем же изданием
    for target in targets:
        try:
            async with chat_lock(connection, target['telegram_chat_id']), connection.transaction():
                chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', target['telegram_chat_id'])
                edition = await bible.chat_translation(connection, target['telegram_chat_id'])
                text = None
                if edition and chat['is_active']:
                    cache_key = (edition['id'], chat['ui_language'])
                    if cache_key not in rendered:
                        rendered[cache_key] = await render_for(connection, edition, refs, chat['ui_language'], draft['image_id'])
                    text = rendered[cache_key]
                if text is None:
                    skipped += 1
                    continue
                await insert_payload(connection, chat, edition, text, f'broadcast:{draft_id}', 'broadcast',
                                     {'kind': 'broadcast', 'broadcast_id': draft_id, 'image_id': draft['image_id']}, max_length=max_length)
                queued += 1
        except Exception as error:
            skipped += 1
            LOGGER.warning('Broadcast %s skipped for one destination (%s)', draft_id, type(error).__name__)
    await connection.execute(
        "UPDATE broadcasts SET status='done',recipients=$2,queued=$3,skipped=$4,updated_at=now() WHERE id=$1",
        draft_id, len(targets), queued, skipped)
    await connection.execute(
        "INSERT INTO operator_events(actor_id,action,details) VALUES($1,'broadcast',$2::jsonb)", actor_id,
        json.dumps({'broadcast_id': draft_id, 'recipients': len(targets), 'queued': queued, 'skipped': skipped}))
    return {'recipients': len(targets), 'queued': queued, 'skipped': skipped}


IMAGE_WAIT = 1800  # секунд: сколько ждать картинку, прежде чем отправить текст без неё


async def media_state(connection: Any, delivery: Any) -> str:
    """Готовность картинки задания рассылки: `ready` (можно отправлять), `wait` (картинка ещё создаётся) или `strip`
    (срок ожидания вышел — отправить без картинки). Задания без картинки всегда `ready`."""
    progress = delivery['progress']
    progress = json.loads(progress) if isinstance(progress, str) else progress
    image = (progress or {}).get('image_id')
    if not image:
        return 'ready'
    status = await connection.fetchval('SELECT status FROM verse_illustrations WHERE id=$1', image)
    if status == 'ready':
        return 'ready'
    waited = await connection.fetchval('SELECT EXTRACT(EPOCH FROM now()-$1)', delivery['created_at'])
    return 'strip' if status in {'failed', None} or waited > IMAGE_WAIT else 'wait'


async def strip_image(connection: Any, delivery: Any) -> None:
    """Убрать картинку из частей задания (текст рассылки уходит без неё)."""
    chunks = delivery['chunks']
    chunks = json.loads(chunks) if isinstance(chunks, str) else chunks
    for chunk in chunks:
        if isinstance(chunk, dict):
            chunk['image_id'] = None
    await connection.execute('UPDATE delivery_log SET chunks=$2::jsonb WHERE id=$1', delivery['id'], json.dumps(chunks))
