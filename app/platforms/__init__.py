"""Общий каркас мессенджеров: описание платформы, идентификаторы, входящая очередь, ограничитель скорости.

Чтобы добавить новый мессенджер, достаточно описать его `PlatformSpec` (см. `builtin.py`) и реализовать три вещи:
клиент API, разбор событий и отправитель сообщений. Всё остальное (очередь событий, идентификаторы, ограничитель,
ключи идемпотентности ответов, общий диспетчер и мост) берётся отсюда. Пошаговая инструкция — docs/development/new-platform.md.
"""
from app.platforms import builtin  # noqa: F401  (регистрирует MAX и VK)
from app.platforms.spec import REGISTRY, PlatformSpec, bridged, get, register

__all__ = ['REGISTRY', 'PlatformSpec', 'bridged', 'get', 'register']
