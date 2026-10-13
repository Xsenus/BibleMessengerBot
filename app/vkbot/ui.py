"""Преобразование общей навигации Telegram в клавиатуры ВКонтакте (inline и постоянная) и HTML в обычный текст."""
from __future__ import annotations

import html
import json
import re

from app.bot.ui import keyboard_command
from app.services.formatting import plain_text

MAX_INLINE_ROWS, MAX_INLINE_BUTTONS, MAX_ROW = 6, 10, 5


def to_text(value: str) -> str:
    """HTML сообщения → обычный текст ВКонтакте: ссылки превращаются в «текст (адрес)», разметка убирается."""
    value = re.sub(r'<a href="([^"]+)">(.*?)</a>',
                   lambda m: f'{m[2]} ({html.unescape(m[1])})' if m[2] != html.escape(html.unescape(m[1])) else html.unescape(m[1]),
                   str(value), flags=re.S)
    try:
        text = plain_text(value)
    except ValueError:
        text = re.sub(r'<[^>]+>', '', value)
    return text.replace('\xa0', ' ')[:4096]


def _button(label: str, payload: str, color: str = 'secondary') -> dict:
    """Callback-кнопка VK: подпись до 40 символов, данные — объект `{"d": данные}` (до 255 байт)."""
    return {'action': {'type': 'callback', 'label': label[:40] or '·', 'payload': json.dumps({'d': payload}, separators=(',', ':'))},
            'color': color}


def fit(rows: list[list[dict]]) -> list[list[dict]]:
    """Первая страница клавиатуры в пределах лимитов VK (остальные кнопки доступны через листание, см. `paginate`)."""
    return paginate(rows)[0] if rows else []


def _is_footer(row: list[dict]) -> bool:
    """Строка «назад / в меню» (подпись начинается со стрелки или домика): её нельзя терять при переполнении."""
    return len(row) <= 2 and all(str(b['action'].get('label', '')).lstrip().startswith(('◀', '⬅', '🏠', '↩')) for b in row)


def paginate(rows: list[list[dict]]) -> list[list[list[dict]]]:
    """Разложить кнопки по страницам inline-клавиатуры VK (не более 6 рядов и 10 кнопок на страницу).

    Ни одна кнопка не теряется: что не поместилось, уходит на следующие страницы. На каждой странице повторяются строка
    озвучки («Слушать») и строка «Назад»; при нескольких страницах добавляется строка листания `‹ 1/3 ›`."""
    rows = [row[:MAX_ROW] for row in rows if row]
    if len(rows) <= MAX_INLINE_ROWS and sum(map(len, rows)) <= MAX_INLINE_BUTTONS:
        return [rows]
    tail = rows[-1] if rows and any(b['action'].get('payload', '').startswith('{"d":"vkaudio:') for b in rows[-1]) else None
    body = rows[:-1] if tail else rows
    footer = body[-1] if body and _is_footer(body[-1]) else None
    body = body[:-1] if footer else body
    fixed_rows = (1 if tail else 0) + (1 if footer else 0) + 1                   # +1 — строка листания
    fixed_buttons = (len(tail) if tail else 0) + (len(footer) if footer else 0) + 3
    rows_cap, buttons_cap = MAX_INLINE_ROWS - fixed_rows, MAX_INLINE_BUTTONS - fixed_buttons
    pieces: list[list[dict]] = []
    for row in body:                                                            # слишком длинная для страницы строка делится
        for i in range(0, len(row), max(1, buttons_cap)):
            pieces.append(row[i:i + max(1, buttons_cap)])
    pages: list[list[list[dict]]] = [[]]
    used = 0
    for piece in pieces:
        if len(pages[-1]) >= rows_cap or used + len(piece) > buttons_cap:
            pages.append([])
            used = 0
        pages[-1].append(piece)
        used += len(piece)
    total = len(pages)
    result = []
    for number, page in enumerate(pages):
        nav = [_button('‹', f'vkpg:{(number - 1) % total}'), _button(f'{number + 1}/{total}', f'vkpg:{number}'),
               _button('›', f'vkpg:{(number + 1) % total}')] if total > 1 else None
        result.append(page + ([nav] if nav else []) + ([footer] if footer else []) + ([tail] if tail else []))
    return result


def keyboard_pages(markup, *, audio: tuple[int, int] | None = None, locale: str = 'ru') -> list[str]:
    """Клавиатуры VK (JSON-строки, по одной на страницу) из inline- или нижней клавиатуры Telegram; при `audio` добавляется кнопка «Слушать»."""
    rows: list[list[dict]] = []
    if markup is not None and hasattr(markup, 'inline_keyboard'):
        for row in markup.inline_keyboard:
            converted = []
            for item in row:
                if item.callback_data:
                    converted.append(_button(item.text, item.callback_data))
                elif item.url and item.url.startswith('https://'):
                    converted.append({'action': {'type': 'open_link', 'link': item.url, 'label': item.text[:40]}})
            if converted:
                rows.append(converted)
    elif markup is not None and hasattr(markup, 'keyboard'):
        single = persistent(markup)
        return [single] if single else []
    if audio:
        rows.append([_button('🔊 Слушать' if locale == 'ru' else '🔊 Listen', f'vkaudio:{audio[0]}:{audio[1]}')])
    return [json.dumps({'inline': True, 'buttons': page}, ensure_ascii=False, separators=(',', ':')) for page in paginate(rows) if page]


def keyboard(markup, *, audio: tuple[int, int] | None = None, locale: str = 'ru') -> str | None:
    """Первая страница клавиатуры VK (см. `keyboard_pages`)."""
    pages = keyboard_pages(markup, audio=audio, locale=locale)
    return pages[0] if pages else None


def persistent(markup) -> str | None:
    """Постоянная клавиатура у поля ввода из нижней клавиатуры Telegram: текстовые кнопки (нажатие приходит как сообщение)."""
    rows = []
    for row in markup.keyboard:
        converted = []
        for item in row:
            if not keyboard_command(item.text) or keyboard_command(item.text) in {'/hide_keyboard', '/donate'}:       # платежей во ВКонтакте нет
                continue
            converted.append({'action': {'type': 'text', 'label': item.text[:40]}, 'color': 'primary'})
        if converted:
            rows.append(converted[:4])
    return json.dumps({'one_time': False, 'inline': False, 'buttons': rows[:10]}, ensure_ascii=False, separators=(',', ':')) if rows else None
