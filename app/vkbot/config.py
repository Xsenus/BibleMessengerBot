"""Настройки бота ВКонтакте: токен сообщества, его номер и версия API. Секреты скрыты из repr."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class VkSettings:
    token: str = field(default='', repr=False)
    group_id: int = 0
    api_version: str = '5.199'
    api_url: str = 'https://api.vk.com/method/'
    service_token: str = field(default='', repr=False)   # необязательный сервисный ключ для чтения видео и стен

    @classmethod
    def from_env(cls, *, require_token: bool = False) -> VkSettings:
        """Прочитать VK_BOT_TOKEN, VK_GROUP_ID, VK_API_VERSION, VK_SERVICE_TOKEN и проверить формат."""
        token = os.getenv('VK_BOT_TOKEN', '').strip()
        group = os.getenv('VK_GROUP_ID', '0').strip() or '0'
        version = os.getenv('VK_API_VERSION', '5.199').strip()
        if require_token and not token:
            raise RuntimeError('VK_BOT_TOKEN is required')
        if token and (len(token) < 20 or any(c.isspace() for c in token)):
            raise ValueError('Invalid VK_BOT_TOKEN format')
        if not group.isdigit():
            raise ValueError('VK_GROUP_ID must be the numeric community id')
        if not re.fullmatch(r'5\.\d{2,3}', version):
            raise ValueError('Invalid VK_API_VERSION')
        return cls(token=token, group_id=int(group), api_version=version,
                   service_token=os.getenv('VK_SERVICE_TOKEN', '').strip())

    @property
    def enabled(self) -> bool:
        """Бот ВКонтакте настроен (есть токен и номер сообщества)."""
        return bool(self.token and self.group_id)
