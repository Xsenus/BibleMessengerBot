"""Платформы мессенджеров: Telegram работает через aiogram напрямую, MAX и ВКонтакте — через мосты."""
from __future__ import annotations

BRIDGED = frozenset({'max', 'vk'})
ALL = ('telegram', 'max', 'vk')


def is_bridged(platform: str | None) -> bool:
    """Платформа обслуживается мостом (MAX, ВКонтакте), а не прямым клиентом Telegram."""
    return platform in BRIDGED
