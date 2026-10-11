"""Включаемые разделы бота (временно скрытые функции)."""
from __future__ import annotations

import os


def support_enabled() -> bool:
    """Показан ли раздел помощи проекту (/donate, /donations, /paysupport, /terms, кнопка «Поддержать»).

    Раздел временно скрыт: включается переменной SUPPORT_SECTION_ENABLED=true. Обработка уже
    поступающих платежей и возвратов не отключается, чтобы не потерять деньги в пути.
    """
    return os.getenv('SUPPORT_SECTION_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
