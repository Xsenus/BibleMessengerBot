"""Bounded MAX REST client with explicit known rejection versus lost acknowledgement."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.maxbot.config import MaxSettings
from app.services.errors import SendError


class MaxAPIError(Exception):
    """Store only status/code; provider response text can contain credentials or user text."""

    def __init__(self, status: int, code: str = '', retry_after: float = 0) -> None:
        self.status = status
        self.code = code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'invalid_response'
        self.retry_after = retry_after
        super().__init__(f'MAX API status {status}: {self.code}')

    def send_error(self, *, editing: bool = False) -> SendError:
        if self.status == 429 or self.code == 'attachment.not.ready':
            return SendError('retry', max(1, self.retry_after))
        if self.status == 401:
            return SendError('retry', 60)  # Credential outage is not a blocked chat.
        if self.status == 403:
            return SendError('forbidden')
        if self.status >= 500 or self.status == 0:
            return SendError('retry', 3) if editing else SendError('uncertain')
        return SendError('rejected')


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise MaxAPIError(0, 'invalid_response') from None
    if not isinstance(data, dict):
        raise MaxAPIError(0, 'invalid_response')
    return data


def _check(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        code = response.json().get('code', 'http_error')
    except (ValueError, AttributeError):
        code = 'http_error'
    try:
        retry_after = min(3600, max(1, float(response.headers.get('Retry-After', '3'))))
    except ValueError:
        retry_after = 3
    raise MaxAPIError(response.status_code, str(code), retry_after)


def validate_body(body: dict) -> None:
    """Enforce MAX text and attachment constraints before a network mutation."""
    text = body.get('text')
    if text is not None and (not isinstance(text, str) or len(text) > 4000):
        raise ValueError('MAX message text exceeds 4000 characters')
    attachments = body.get('attachments') or []
    if any(a.get('type') in {'audio', 'file'} for a in attachments):
        media = [a for a in attachments if a.get('type') != 'inline_keyboard']
        if len(media) != 1 or len(attachments) > 2:
            raise ValueError('MAX audio/file supports only one media and one keyboard')
    if not text and not attachments:
        raise ValueError('Empty MAX message')


class MaxClient:
    """Do not retry mutation requests automatically or log token-bearing upload URLs."""

    def __init__(self, config: MaxSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=config.api_url, headers={'Authorization': config.token},
            timeout=httpx.Timeout(25, connect=10), verify=config.tls_context(),
            follow_redirects=False, transport=transport,
        )
        # Separate client prevents API Authorization from leaking to upload hosts.
        self.upload_http = httpx.AsyncClient(timeout=120, verify=config.tls_context(),
                                             follow_redirects=False, transport=transport)

    async def close(self) -> None:
        await self.http.aclose()
        await self.upload_http.aclose()

    async def request(self, method: str, path: str, *, params=None, body=None) -> dict:
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('MAX API path must be relative')
        try:
            response = await self.http.request(method, path, params=params, json=body)
        except (httpx.HTTPError, OSError):
            raise MaxAPIError(0, 'network') from None
        _check(response)
        return _json(response)

    async def get_me(self) -> dict:
        return await self.request('GET', '/me')

    async def send(self, *, chat_id: int | None = None, user_id: int | None = None,
                   body: dict) -> str:
        if (chat_id is None) == (user_id is None):
            raise ValueError('Select exactly one MAX destination')
        validate_body(body)
        data = await self.request('POST', '/messages',
            params={'chat_id': chat_id} if chat_id is not None else {'user_id': user_id}, body=body)
        message = data.get('message', {}).get('body', {}).get('mid')
        if not isinstance(message, str) or not 1 <= len(message) <= 256:
            raise MaxAPIError(0, 'missing_message_id')
        return message

    async def edit(self, message_id: str, body: dict) -> None:
        validate_body(body)
        result = await self.request('PUT', '/messages', params={'message_id': message_id}, body=body)
        if result.get('success') is not True:
            raise MaxAPIError(400, 'edit_rejected')

    async def answer(self, callback_id: str, notification: str | None = None) -> None:
        result = await self.request('POST', '/answers', params={'callback_id': callback_id},
                                    body={'notification': notification})
        if result.get('success') is not True:
            raise MaxAPIError(400, 'callback_rejected')

    async def commands(self, commands: list[dict]) -> None:
        if len(commands) > 32 or any(not re.fullmatch(r'[a-z_]{1,64}', c['name'])
                or not 1 <= len(c.get('description', '')) <= 128 for c in commands):
            raise ValueError('Invalid MAX command menu')
        result = await self.request('PATCH', '/me/commands', body={'commands': commands})
        if result.get('commands') != commands:
            raise MaxAPIError(400, 'commands_rejected')

    async def subscribe(self, update_types: list[str]) -> None:
        if not self.config.webhook_url or not self.config.webhook_secret:
            raise ValueError('MAX webhook URL and secret are required')
        result = await self.request('POST', '/subscriptions', body={
            'url': self.config.webhook_url, 'secret': self.config.webhook_secret,
            'update_types': update_types,
        })
        if result.get('success') is not True:
            raise MaxAPIError(400, 'subscription_rejected')

    async def upload(self, kind: str, data: bytes, filename: str, mime_type: str) -> dict:
        if kind not in {'image', 'audio'} or not data:
            raise ValueError('Unsupported MAX media')
        if len(data) > (50 if kind == 'image' else 256) * 1024 * 1024:
            raise ValueError('MAX media exceeds upload limit')
        endpoint = await self.request('POST', '/uploads', params={'type': kind})
        raw_url = endpoint.get('url', '')
        url = urlsplit(raw_url)
        # Official image/audio upload domains. Never accept HTTP, local destinations,
        # user-info, alternative ports or redirects from a malformed API response.
        host = (url.hostname or '').lower()
        if (url.scheme != 'https' or url.port not in {None, 443} or url.username or url.password
                or not any(host.endswith('.' + root) for root in ('oneme.ru', 'okcdn.ru', 'max.ru'))):
            raise MaxAPIError(0, 'invalid_upload_endpoint')
        try:
            response = await self.upload_http.post(raw_url, files={'data': (filename, data, mime_type)})
        except (httpx.HTTPError, OSError):
            # Upload creates no visible message. A later controlled retry is safe.
            raise MaxAPIError(429, 'upload_network', 3) from None
        _check(response)
        if kind == 'image':
            photos = _json(response).get('photos')
            if (not isinstance(photos, dict) or not photos
                    or any(not isinstance(p, dict) or not isinstance(p.get('token'), str)
                           or not p['token'] for p in photos.values())):
                raise MaxAPIError(429, 'invalid_upload_response', 3)
            return {'photos': photos}
        # The audio endpoint returns XML, not JSON, and the token comes from /uploads.
        if not re.fullmatch(r'\s*<retval>1</retval>\s*', response.text):
            raise MaxAPIError(429, 'upload_rejected', 3)
        token = endpoint.get('token')
        if not isinstance(token, str) or not token:
            raise MaxAPIError(429, 'missing_upload_token', 3)
        return {'token': token}
