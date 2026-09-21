"""Small domain errors with stable localization keys and transport failure kinds."""
from __future__ import annotations


class UserError(ValueError):
    """An expected user-facing failure; key is looked up in the destination locale."""
    def __init__(self, key: str, detail: str = '') -> None:
        self.key = key
        self.detail = detail
        super().__init__(key)


class SendError(Exception):
    """A send outcome: retry is definitely refused; uncertain may already be delivered."""
    def __init__(self, kind: str, retry_after: float = 0.0) -> None:
        if kind not in {'retry', 'forbidden', 'rejected', 'uncertain'}:
            raise ValueError('Invalid send error kind')
        self.kind = kind
        self.retry_after = max(0.0, retry_after)
        super().__init__(kind)
