"""Читательские функции: закладки с заметками, отметки чтения и серия дней подряд.

Всё хранится по чату (личному или групповому), как и прогресс чтения. Закладка привязана к адресу
(книга, глава, стихи), а не к тексту издания, поэтому показывается в издании чата на момент просмотра.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.services import bible, passage
from app.services.errors import UserError
from app.services.formatting import escape
from app.services.locks import chat_lock

MAX_BOOKMARKS = 200
MAX_NOTE = 300
BOOKMARK_PAGE_SIZE = 10


def split_reference_and_note(text: str) -> tuple[str, str]:
    """Разделить адрес и заметку, сохраняя многословные названия и номера книг."""
    text = text.strip()
    match = re.match(r'^(.+?\s*[1-9][0-9]{0,2}(?:\s*[:.,]\s*[1-9][0-9]{0,2}(?:\s*[-–—]\s*[1-9][0-9]{0,2})?)?)(?:\s+(.*))?$', text, re.S)
    if not match or passage.parse_reference(match.group(1)) is None:
        return text, ''
    return match.group(1).strip(), (match.group(2) or '').strip()[:MAX_NOTE]


async def add_bookmark(connection: Any, chat_id: int, edition: Any, text: str) -> tuple[int, str]:
    """Добавить закладку по адресу из текста (необязательная заметка после адреса). Возвращает (номер, подпись)."""
    query, note = split_reference_and_note(text)
    result = await passage.lookup(connection, edition, query)
    if result is None:
        raise UserError('invalid')
    reference, book, _rows = result
    first = reference.first or 0
    last = reference.last or first
    async with chat_lock(connection, chat_id), connection.transaction():
        if await connection.fetchval('SELECT count(*) FROM bookmarks WHERE telegram_chat_id=$1', chat_id) >= MAX_BOOKMARKS:
            existing = await connection.fetchval('''SELECT id FROM bookmarks WHERE telegram_chat_id=$1
                AND book_code=$2 AND chapter=$3 AND verse_from=$4 AND verse_to=$5''', chat_id, book, reference.chapter, first, last)
            if existing is None:
                raise UserError('limit')
        identifier = await connection.fetchval(
            """INSERT INTO bookmarks(telegram_chat_id,book_code,chapter,verse_from,verse_to,note) VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT(telegram_chat_id,book_code,chapter,verse_from,verse_to) DO UPDATE SET note=EXCLUDED.note RETURNING id""",
            chat_id, book, reference.chapter, first, last, note)
    return identifier, await label(connection, edition, book, reference.chapter, first, last)


async def label(connection: Any, edition: Any, book: str, chapter: int, first: int, last: int) -> str:
    """Подпись адреса в издании: `Иоанна 3:16–18`."""
    name = await bible._book_name(connection, book, edition['language_code'], edition['id'])
    if not first:
        return f'{name} {chapter}'
    return f'{name} {chapter}:{first}' + (f'–{last}' if last != first else '')


async def listing(connection: Any, chat_id: int, edition: Any, ru: bool = True, *, page: int = 1) -> str:
    """Страница закладок чата; ссылки на соседние страницы работают на всех платформах."""
    total = await connection.fetchval('SELECT count(*) FROM bookmarks WHERE telegram_chat_id=$1', chat_id)
    pages = max(1, (total + BOOKMARK_PAGE_SIZE - 1) // BOOKMARK_PAGE_SIZE)
    if not 1 <= page <= pages:
        raise UserError('invalid')
    rows = await connection.fetch('''SELECT * FROM bookmarks WHERE telegram_chat_id=$1
        ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3''', chat_id, BOOKMARK_PAGE_SIZE, (page - 1) * BOOKMARK_PAGE_SIZE)
    if not rows:
        return 'Закладок пока нет. Добавьте: <code>/bookmark Ин 3:16 заметка</code>' if ru else 'No bookmarks yet. Add one: <code>/bookmark John 3:16 note</code>'
    lines = ['🔖 <b>' + ('Закладки' if ru else 'Bookmarks') + '</b>']
    for row in rows:
        title = await label(connection, edition, row['book_code'], row['chapter'], row['verse_from'], row['verse_to'])
        note = f" — {escape(row['note'])}" if row['note'] else ''
        lines.append(f"<code>{row['id']}</code> · {escape(title)}{note}")
    if pages > 1:
        lines.append(f'Страница {page}/{pages} · Всего: {total}' if ru else f'Page {page}/{pages} · Total: {total}')
        if page > 1:
            lines.append(('← Назад: ' if ru else '← Previous: ') + f'<code>/bookmarks {page - 1}</code>')
        if page < pages:
            lines.append(('Далее →: ' if ru else 'Next →: ') + f'<code>/bookmarks {page + 1}</code>')
    lines.append('Удалить: <code>/unbookmark НОМЕР</code>' if ru else 'Remove: <code>/unbookmark NUMBER</code>')
    return '\n'.join(lines)


async def remove_bookmark(connection: Any, chat_id: int, bookmark_id: int) -> bool:
    return await connection.execute('DELETE FROM bookmarks WHERE id=$1 AND telegram_chat_id=$2', bookmark_id, chat_id) == 'DELETE 1'


async def mark_read(connection: Any, chat_id: int, day: date) -> bool:
    """Отметить день прочитанным; True, если отметка новая."""
    return await connection.execute(
        'INSERT INTO reading_marks(telegram_chat_id,day) VALUES($1,$2) ON CONFLICT DO NOTHING', chat_id, day) == 'INSERT 0 1'


async def streak(connection: Any, chat_id: int, today: date) -> tuple[int, int, int]:
    """(текущая серия дней подряд, лучшая серия, всего дней). Серия не обрывается, пока сегодня ещё можно отметиться."""
    days = [r['day'] for r in await connection.fetch('SELECT day FROM reading_marks WHERE telegram_chat_id=$1 ORDER BY day', chat_id)]
    if not days:
        return 0, 0, 0
    best = run = 1
    for before, after in zip(days, days[1:], strict=False):
        run = run + 1 if after - before == timedelta(days=1) else 1
        best = max(best, run)
    current = 0
    marked = set(days)
    cursor = today if today in marked else today - timedelta(days=1)
    while cursor in marked:
        current += 1
        cursor -= timedelta(days=1)
    return current, best, len(days)


def streak_text(current: int, best: int, total: int, ru: bool = True) -> str:
    if ru:
        return f'🔥 Серия: <b>{current}</b> дн. подряд\nЛучшая серия: {best}\nВсего дней чтения: {total}'
    return f'🔥 Streak: <b>{current}</b> days in a row\nBest streak: {best}\nTotal reading days: {total}'
