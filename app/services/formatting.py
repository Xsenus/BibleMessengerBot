"""Telegram-safe text rendering helpers."""

from __future__ import annotations

import html


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Split a long message, preferring paragraph and line boundaries."""
    if limit < 100:
        raise ValueError("limit must be at least 100")
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remainder = text
    while len(remainder) > limit:
        cut = remainder.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remainder.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remainder.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunk = remainder[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remainder = remainder[cut:].strip()
    if remainder:
        chunks.append(remainder)
    return chunks
