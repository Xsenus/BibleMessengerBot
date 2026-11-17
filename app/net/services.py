"""Внешние сервисы, для которых настраиваются маршруты."""
from __future__ import annotations
from urllib.parse import urlsplit, urlunsplit

# ключ -> (название, адрес для проверки доступности; любой HTTP-ответ означает, что путь работает)
SERVICES: dict[str, tuple[str, str]] = {
    'telegram': ('Telegram', 'https://api.telegram.org'),
    'max': ('MAX', 'https://platform-api2.max.ru'),
    'vk': ('ВКонтакте', 'https://api.vk.com'),
    'youtube': ('YouTube', 'https://www.youtube.com'),
    'rutube': ('RuTube', 'https://rutube.ru'),
    'images': ('Генераторы картинок', 'https://api.openai.com'),
    'sources': ('Источники Библии и прочие сайты', 'https://www.google.com'),
}

# Окончания имён хостов -> сервис (для автоматического выбора маршрута по адресу запроса).
HOSTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (('telegram.org', 't.me'), 'telegram'),
    (('max.ru', 'oneme.ru', 'okcdn.ru'), 'max'),
    (('vk.com', 'vk.ru', 'userapi.com', 'vkuser.net', 'vkuseraudio.net'), 'vk'),
    (('youtube.com', 'youtu.be', 'googlevideo.com', 'ytimg.com'), 'youtube'),
    (('rutube.ru',), 'rutube'),
    (('openai.com', 'googleapis.com', 'bfl.ai', 'ideogram.ai', 'stability.ai', 'cloudflare.com', 'huggingface.co',
      'pollinations.ai', 'aihorde.net', 'together.xyz', 'siliconflow.com'), 'images'),
)


def service_for_host(host: str) -> str:
    """Сервис по имени хоста; неизвестные хосты относятся к `sources` (прочие сайты)."""
    host = (host or '').lower().rstrip('.')
    for suffixes, service in HOSTS:
        if any(host == s or host.endswith('.' + s) for s in suffixes):
            return service
    return 'sources'


def telegram_proxy_url(url: str | None) -> str | None:
    """Привести адрес к схеме, которую поддерживает сессия Telegram."""
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme == 'socks5h':
        return urlunsplit(parts._replace(scheme='socks5'))
    if parts.scheme not in {'http', 'socks5'}:
        raise ValueError('unsupported_telegram_proxy_scheme')
    return url
