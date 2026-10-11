"""Необязательные настройки MAX; секреты не попадают в представление объекта."""
from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MaxSettings:
    """Настройки MAX: токен бота, секрет и адрес вебхука, файл доверенного сертификата и адрес API."""
    token: str = field(default='', repr=False)
    webhook_secret: str = field(default='', repr=False)
    webhook_url: str = ''
    ca_file: Path | None = None
    api_url: str = 'https://platform-api2.max.ru'

    @classmethod
    def from_env(cls, *, require_token: bool = False) -> MaxSettings:
        """Прочитать настройки из переменных MAX_* и проверить формат токена, секрета (24–256 URL-безопасных символов) и адреса вебхука (HTTPS без порта)."""
        config = cls(
            token=os.getenv('MAX_BOT_TOKEN', '').strip(),
            webhook_secret=os.getenv('MAX_WEBHOOK_SECRET', '').strip(),
            webhook_url=os.getenv('MAX_WEBHOOK_URL', '').strip(),
            ca_file=Path(os.environ['MAX_CA_FILE']) if os.getenv('MAX_CA_FILE') else None,
        )
        if require_token and not config.token:
            raise RuntimeError('MAX_BOT_TOKEN is required')
        if config.token and (len(config.token) < 16 or any(c.isspace() for c in config.token)):
            raise ValueError('Invalid MAX_BOT_TOKEN format')
        if config.webhook_secret and not re.fullmatch(r'[A-Za-z0-9_-]{24,256}', config.webhook_secret):
            raise ValueError('MAX_WEBHOOK_SECRET must contain 24-256 URL-safe characters')
        if config.webhook_url:
            url = urlsplit(config.webhook_url)
            if (url.scheme != 'https' or not url.hostname or url.port is not None
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError('MAX_WEBHOOK_URL must be an HTTPS URL on implicit port 443')
        return config

    def tls_context(self) -> ssl.SSLContext:
        """Контекст TLS: системные сертификаты плюс явно заданный корневой сертификат; проверка никогда не отключается."""
        context = ssl.create_default_context()
        if self.ca_file:
            context.load_verify_locations(cafile=str(self.ca_file))
        return context
