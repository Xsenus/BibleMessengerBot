"""Встроенные платформы: MAX и ВКонтакте (Telegram работает напрямую, без моста)."""
from __future__ import annotations

from app.platforms.spec import PlatformSpec, register
from app.services.formatting import split_message, split_wire_message


def _max_parts(text: str, limit: int) -> list:
    """MAX: части `max_text` с ограничением сырого HTML."""
    return [dict(kind='max_text', text=part) for part in split_wire_message(text, min(limit, 3600))]


def _vk_parts(text: str, limit: int) -> list:
    """ВКонтакте: обычные строки до 4000 символов (текст потом приводится к простому)."""
    return split_message(text, min(limit, 4000))


MAX = register(PlatformSpec(name='max', title='MAX', service='maxbot', inbox_table='max_inbox', rate_table='max_rate_limits',
                            global_gap=0.1, split=_max_parts, chunk_kind='max_text'))
VK = register(PlatformSpec(name='vk', title='VK', service='vkbot', inbox_table='vk_inbox', rate_table='vk_rate_limits',
                           global_gap=0.07, split=_vk_parts, chunk_kind='vk_text'))
