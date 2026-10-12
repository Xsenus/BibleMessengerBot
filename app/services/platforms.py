"""Платформы мессенджеров: Telegram работает через aiogram напрямую, остальные (MAX, ВКонтакте и добавляемые позже) — через мосты.

Список мостовых платформ берётся из реестра `app.platforms`: новая платформа появляется здесь автоматически."""
from __future__ import annotations

from app.platforms import bridged
from app.platforms import builtin  # noqa: F401  (регистрация встроенных платформ)

BRIDGED = bridged()
ALL = ('telegram', *sorted(BRIDGED, key=lambda name: (name != 'max', name)))


def is_bridged(platform: str | None) -> bool:
    """Платформа обслуживается мостом (MAX, ВКонтакте…), а не прямым клиентом Telegram."""
    return platform in BRIDGED
