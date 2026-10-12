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

MAX_BOOKMARKS = 200
MAX_NOTE = 300


def split_reference_and_note(text: str) -> tuple[str, str]:
    """Разделить `Ин 3:16 важный стих` на адрес и заметку: адрес — до первого слова, которое не похоже на его часть."""
    text = text.strip()
    match = re.match(r'^(\d?\s?[^\W\d_]+\.?\s+\d+(?:\s*[:.,]\s*\d+(?:\s*[-–—]\s*\d+)?)?)(?:\s+(.*))?$', text, re.S)
    if not match:
        return text, ''
    return match.group(1).strip(), (match.group(2) or '').strip()[:MAX_NOTE]


async def add_bookmark(connection: Any, chat_id: int, edition: Any, text: str) -> tuple[int, str]:
    """Добавить закладку по адресу из текста (необязательная заметка после адреса). Возвращает (номер, подпись)."""
    query, note = split_reference_and_note(text)
    result = await passage.lookup(connection, edition, query)
    if result is None:
        raise UserError('invalid')
    reference, book, _rows = result
    if await connection.fetchval('SELECT count(*) FROM bookmarks WHERE telegram_chat_id=$1', chat_id) >= MAX_BOOKMARKS:
        raise UserError('limit')
    first = reference.first or 0
    last = reference.last or first
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


async def listing(connection: Any, chat_id: int, edition: Any, ru: bool = True) -> str:
    """Список закладок чата."""
    rows = await connection.fetch('SELECT * FROM bookmarks WHERE telegram_chat_id=$1 ORDER BY created_at DESC,id DESC LIMIT 50', chat_id)
    if not rows:
        return 'Закладок пока нет. Добавьте: <code>/bookmark Ин 3:16 заметка</code>' if ru else 'No bookmarks yet. Add one: <code>/bookmark John 3:16 note</code>'
    lines = ['🔖 <b>' + ('Закладки' if ru else 'Bookmarks') + '</b>']
    for row in rows:
        title = await label(connection, edition, row['book_code'], row['chapter'], row['verse_from'], row['verse_to'])
        note = f" — {escape(row['note'])}" if row['note'] else ''
        lines.append(f"<code>{row['id']}</code> · {escape(title)}{note}")
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
