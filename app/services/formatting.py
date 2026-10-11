"""Разбор и разбиение поддерживаемого подмножества HTML Telegram без поломанных тегов и сущностей."""
from __future__ import annotations

import html
from dataclasses import dataclass
from html.parser import HTMLParser


def escape(value: object) -> str:
    """Экранировать недоверенный текст (он никогда не используется как разметка)."""
    return html.escape(str(value), quote=False)


def utf16_length(value: str) -> int:
    """Длина текста в единицах UTF-16 — так Telegram считает лимиты (символы вне BMP занимают две единицы)."""
    return len(value.encode('utf-16-le')) // 2


@dataclass(frozen=True)
class Character:
    """Один видимый символ и стек тегов разметки, действующих на него."""
    text: str
    stack: tuple[tuple[str, str], ...]


class _Parser(HTMLParser):
    """Разборщик допустимого подмножества HTML (`b`, `i`, `code`, `blockquote`, `a` с HTTP(S)-ссылкой, вложенность до 8)."""
    def __init__(self) -> None:
        """Подготовить стек тегов и список символов."""
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.characters: list[Character] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Открывающий тег: проверить допустимость, отсутствие лишних атрибутов и безопасность ссылки, добавить в стек."""
        if tag not in {'b', 'i', 'code', 'blockquote', 'a'}:
            raise ValueError(f'Unsupported Telegram HTML tag: {tag}')
        if tag == 'a':
            values = dict(attrs)
            href = values.get('href', '') or ''
            if len(attrs) != 1 or not href.startswith(('https://', 'http://')):
                raise ValueError('Only HTTP(S) source links are allowed')
            opening = f'<a href="{html.escape(href, quote=True)}">'
        else:
            if attrs:
                raise ValueError(f'Unexpected attributes on {tag}')
            opening = f'<{tag}>'
        self.stack.append((tag, opening))
        if len(self.stack) > 8:
            raise ValueError('HTML nesting is too deep')

    def handle_endtag(self, tag: str) -> None:
        """Закрывающий тег: должен совпадать с последним открытым."""
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError(f'Unbalanced HTML closing tag: {tag}')
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        """Текст: каждому символу сопоставляется текущий стек тегов."""
        self.characters.extend(Character(c, tuple(self.stack)) for c in data)

    def handle_comment(self, data: str) -> None:
        """Комментарии HTML не поддерживаются."""
        raise ValueError('HTML comments are not supported')

    def handle_decl(self, decl: str) -> None:
        """Объявления HTML не поддерживаются."""
        raise ValueError('HTML declarations are not supported')


def _characters(text: str) -> list[Character]:
    """Разобрать HTML в список символов со стеками; незакрытые теги — ошибка."""
    parser = _Parser()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError('Unclosed HTML tags')
    return parser.characters


def plain_text(text: str) -> str:
    """Видимый текст без разметки (заодно проверяет корректность HTML)."""
    return ''.join(item.text for item in _characters(text))


def _render(characters: list[Character]) -> str:
    """Собрать HTML обратно из символов, закрывая и заново открывая теги там, где меняется стек."""
    result: list[str] = []
    current: tuple[tuple[str, str], ...] = ()
    for item in characters:
        common = 0
        while common < min(len(current), len(item.stack)) and current[common] == item.stack[common]:
            common += 1
        result.extend(f'</{tag}>' for tag, _ in reversed(current[common:]))
        result.extend(opening for _, opening in item.stack[common:])
        result.append(escape(item.text))
        current = item.stack
    result.extend(f'</{tag}>' for tag, _ in reversed(current))
    return ''.join(result)


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Разбить текст на части по лимиту единиц UTF-16, по возможности по пробелам; форматирование заново открывается в каждой части, слова и символы не режутся и не теряются, пробелы на границе убираются."""
    if not 100 <= limit <= 4096:
        raise ValueError('limit must be between 100 and 4096')
    characters = _characters(text.strip())
    chunks: list[str] = []
    start = 0
    while start < len(characters):
        while start < len(characters) and characters[start].text.isspace():
            start += 1
        end, size, boundary = start, 0, None
        while end < len(characters):
            units = utf16_length(characters[end].text)
            if size + units > limit:
                break
            size += units
            if size >= limit // 2 and characters[end].text.isspace():
                boundary = end
            end += 1
        if end < len(characters) and boundary is not None:
            end = boundary
        stop = end
        while stop > start and characters[stop - 1].text.isspace():
            stop -= 1
        if stop > start:
            chunks.append(_render(characters[start:stop]))
        start = max(end, start + 1)
    return chunks


def split_wire_message(text: str, limit: int = 3600) -> list[str]:
    """Разбить текст так, чтобы длина сырого HTML (с повторно открытыми тегами и экранированием) не превышала лимит; все видимые символы, включая пробелы на границах, сохраняются (для MAX)."""
    if not 100 <= limit <= 4000:
        raise ValueError('Wire limit must be between 100 and 4000')
    characters = _characters(text)
    chunks, start = [], 0
    while start < len(characters):
        low, high = start + 1, min(len(characters), start + limit)
        end = start
        while low <= high:
            middle = (low + high) // 2
            if len(_render(characters[start:middle])) <= limit:
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end == start:
            raise ValueError('HTML markup exceeds the wire limit')
        if end < len(characters):
            boundaries = [i + 1 for i in range(start + (end-start)//2, end)
                          if characters[i].text.isspace()]
            if boundaries:
                end = boundaries[-1]
        chunks.append(_render(characters[start:end]))
        start = end
    return chunks
