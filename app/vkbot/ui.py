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
    """Уложить кнопки в ограничения inline-клавиатуры VK (6 строк, 5 в ряду, не более 10 кнопок): лишние строки отбрасываются с конца,
    а последняя строка (озвучка) сохраняется."""
    rows = [row[:MAX_ROW] for row in rows if row]
    if len(rows) <= MAX_INLINE_ROWS and sum(map(len, rows)) <= MAX_INLINE_BUTTONS:
        return rows
    tail = rows[-1] if rows and any(b['action'].get('payload', '').startswith('{"d":"vkaudio:') for b in rows[-1]) else None
    body = rows[:-1] if tail else rows
    kept: list = []
    total = len(tail) if tail else 0
    for row in body:
        if len(kept) + (1 if tail else 0) >= MAX_INLINE_ROWS or total + len(row) > MAX_INLINE_BUTTONS:
            break
        kept.append(row)
        total += len(row)
    return kept + ([tail] if tail else [])


def keyboard(markup, *, audio: tuple[int, int] | None = None, locale: str = 'ru') -> str | None:
    """Клавиатура VK (JSON-строка) из inline- или нижней клавиатуры Telegram; при `audio` добавляется кнопка «Слушать»."""
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
        return persistent(markup)
    if audio:
        rows.append([_button('🔊 Слушать' if locale == 'ru' else '🔊 Listen', f'vkaudio:{audio[0]}:{audio[1]}')])
    rows = fit(rows)
    return json.dumps({'inline': True, 'buttons': rows}, ensure_ascii=False, separators=(',', ':')) if rows else None


def persistent(markup) -> str | None:
    """Постоянная клавиатура у поля ввода из нижней клавиатуры Telegram: текстовые кнопки (нажатие приходит как сообщение)."""
    rows = []
    for row in markup.keyboard:
        converted = []
        for item in row:
            if not keyboard_command(item.text) or keyboard_command(item.text) == '/hide_keyboard':
                continue
            converted.append({'action': {'type': 'text', 'label': item.text[:40]}, 'color': 'primary'})
        if converted:
            rows.append(converted[:4])
    return json.dumps({'one_time': False, 'inline': False, 'buttons': rows[:10]}, ensure_ascii=False, separators=(',', ':')) if rows else None
