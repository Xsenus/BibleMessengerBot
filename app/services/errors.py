"""Небольшие предметные ошибки с устойчивыми ключами перевода и видами сбоев отправки."""
from __future__ import annotations


class UserError(ValueError):
    """Ожидаемая ошибка для пользователя; ключ ищется в каталоге перевода чата."""
    def __init__(self, key: str, detail: str = '') -> None:
        """Запомнить ключ сообщения и необязательную подробность."""
        self.key = key
        self.detail = detail
        super().__init__(key)


class SendError(Exception):
    """Результат неудачной отправки: `retry` (временно, повторить), `forbidden` (запрещено — чат отключается), `rejected` (отклонено) и `uncertain` (исход неизвестен — сообщение могло дойти)."""
    def __init__(self, kind: str, retry_after: float = 0.0) -> None:
        """Проверить вид ошибки и запомнить его вместе со временем ожидания повтора."""
        if kind not in {'retry', 'forbidden', 'rejected', 'uncertain'}:
            raise ValueError('Invalid send error kind')
        self.kind = kind
        self.retry_after = max(0.0, retry_after)
        super().__init__(kind)
