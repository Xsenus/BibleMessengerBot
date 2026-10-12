"""Клиент API ВКонтакте с ограничениями: известный отказ отличается от потерянного подтверждения.

Все методы вызываются POST-запросом с токеном сообщества. Изменяющие запросы автоматически не повторяются.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from app.net.transport import SmartTransport
from app.services.errors import SendError
from app.vkbot.config import VkSettings

# Коды ошибок VK, при которых сообщение пользователю не доставить (нет согласия, закрыт диалог, бот удалён из беседы).
FORBIDDEN = {7, 15, 900, 901, 902, 912, 917, 920, 925, 945, 946}
RETRY = {6, 9, 10, 14, 29}
AUTH = {5, 27, 28}
MIN_INTERVAL = 0.06   # секунд между вызовами API из одного процесса


class VkAPIError(Exception):
    """Ошибка API VK: хранится только код и безопасное сообщение (без тел ответов, где могут быть секреты)."""

    def __init__(self, code: int, message: str = '', retry_after: float = 0) -> None:
        self.code = code
        self.message = re.sub(r'[^\w .,:;()\-/]', '', message)[:200]
        self.retry_after = retry_after
        super().__init__(f'VK API error {code}')

    def send_error(self, *, editing: bool = False) -> SendError:
        """Преобразовать ошибку API в SendError для очереди доставки."""
        if self.code in FORBIDDEN:
            return SendError('forbidden')
        if self.code in AUTH:
            return SendError('retry', 60)
        if self.code in RETRY:
            return SendError('retry', max(3, self.retry_after))
        if self.code == 0:  # сеть или неожиданный ответ
            return SendError('retry', 3) if editing else SendError('uncertain')
        return SendError('rejected')


class VkClient:
    """Асинхронный клиент ВКонтакте: методы API, Long Poll и загрузка файлов."""

    def __init__(self, settings: VkSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(base_url=settings.api_url, timeout=httpx.Timeout(30, connect=10),
                                      follow_redirects=False,
                                      transport=transport or SmartTransport('vk'))
        # Отдельный клиент для загрузки файлов и Long Poll: токен API на сторонние хосты не уходит.
        self._pace_lock = asyncio.Lock()
        self._last_call = 0.0
        self.raw = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
                                     transport=transport or SmartTransport('vk'))

    async def close(self) -> None:
        """Закрыть HTTP-клиенты."""
        await self.http.aclose()
        await self.raw.aclose()

    async def call(self, method: str, token: str | None = None, **params: Any) -> Any:
        """Вызвать метод API и вернуть поле `response`; ошибки VK превращаются в VkAPIError."""
        if not re.fullmatch(r'[a-zA-Z]+\.[a-zA-Z]+', method):
            raise ValueError('Invalid VK method')
        async with self._pace_lock:                      # все вызовы API (не только отправка) делят лимит ~20 запросов в секунду
            wait = self._last_call + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        data = {k: v for k, v in params.items() if v is not None}
        data.update(access_token=token or self.settings.token, v=self.settings.api_version)
        try:
            response = await self.http.post(method, data=data)
        except (httpx.HTTPError, OSError):
            raise VkAPIError(0, 'network') from None
        if response.status_code >= 500:
            raise VkAPIError(0, 'server')
        try:
            body = response.json()
        except ValueError:
            raise VkAPIError(0, 'invalid response') from None
        if not isinstance(body, dict):
            raise VkAPIError(0, 'invalid response')
        if 'error' in body:
            error = body['error'] if isinstance(body['error'], dict) else {}
            raise VkAPIError(int(error.get('error_code', 0) or 0), str(error.get('error_msg', '')))
        if 'response' not in body:
            raise VkAPIError(0, 'invalid response')
        return body['response']

    async def get_me(self) -> dict:
        """Данные сообщества, от имени которого работает токен."""
        groups = await self.call('groups.getById', group_id=self.settings.group_id)
        items = groups.get('groups', groups) if isinstance(groups, dict) else groups
        return items[0]

    async def configure_long_poll(self) -> None:
        """Включить Long Poll сообщества и нужные типы событий (сообщения, нажатия, посты на стене, подписки)."""
        await self.call('groups.setLongPollSettings', group_id=self.settings.group_id, enabled=1,
                        api_version=self.settings.api_version, message_new=1, message_event=1, message_allow=1,
                        message_deny=1, wall_post_new=1, group_join=0, group_leave=0)

    async def long_poll_server(self) -> dict:
        """Получить адрес, ключ и текущий номер события сервера Long Poll."""
        return await self.call('groups.getLongPollServer', group_id=self.settings.group_id)

    async def poll(self, server: dict, wait: int = 25) -> dict:
        """Один запрос к Long Poll: вернуть {ts, updates} или {failed: код}. Сетевой сбой — VkAPIError(0)."""
        from urllib.parse import urlsplit
        url = urlsplit(str(server['server']))
        if url.scheme != 'https' or not (url.hostname or '').endswith(('vk.com', 'vk.ru', 'userapi.com')):
            raise VkAPIError(0, 'unexpected long poll host')
        try:
            response = await self.raw.get(str(server['server']), params={'act': 'a_check', 'key': server['key'],
                                                                         'ts': server['ts'], 'wait': wait},
                                          timeout=httpx.Timeout(wait + 15, connect=10))
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            raise VkAPIError(0, 'network') from None
        if not isinstance(body, dict):
            raise VkAPIError(0, 'invalid response')
        return body

    async def send(self, peer_id: int, *, message: str = '', attachment: str | None = None, keyboard: str | None = None,
                   reply_to_cmid: int | None = None) -> int:
        """Отправить сообщение и вернуть его номер в беседе (conversation_message_id) — он нужен для правок."""
        import random
        import json
        # Ответ на сообщение по номеру в беседе (conversation_message_id) делается через forward с is_reply.
        forward = json.dumps({'peer_id': peer_id, 'conversation_message_ids': [reply_to_cmid], 'is_reply': True}) if reply_to_cmid else None
        result = await self.call('messages.send', peer_ids=peer_id, random_id=random.randint(1, 2**31 - 1),
                                 message=message or None, attachment=attachment, keyboard=keyboard,
                                 dont_parse_links=1, forward=forward)
        item = result[0] if isinstance(result, list) and result else None
        cmid = (item or {}).get('conversation_message_id')
        if not isinstance(cmid, int) or cmid <= 0 or (item or {}).get('error'):
            raise VkAPIError(int(((item or {}).get('error') or {}).get('code', 0) or 0), 'send rejected')
        return cmid

    async def edit(self, peer_id: int, cmid: int, *, message: str = '', attachment: str | None = None,
                   keyboard: str | None = None) -> None:
        """Отредактировать своё сообщение по номеру в беседе."""
        await self.call('messages.edit', peer_id=peer_id, conversation_message_id=cmid, message=message or ' ',
                        attachment=attachment, keyboard=keyboard, dont_parse_links=1)

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str | None = None) -> None:
        """Подтвердить нажатие callback-кнопки (необязательное всплывающее уведомление)."""
        import json
        data = json.dumps({'type': 'show_snackbar', 'text': text[:90]}) if text else None
        await self.call('messages.sendMessageEventAnswer', event_id=event_id, user_id=user_id, peer_id=peer_id, event_data=data)

    async def upload(self, kind: str, peer_id: int, data: bytes, filename: str, mime_type: str) -> str:
        """Загрузить картинку (`photo`) или документ (`doc`) для сообщений и вернуть строку вложения `photo-1_2_ключ`."""
        if kind not in {'photo', 'doc', 'voice'} or not data or len(data) > 50 * 1024 * 1024:
            raise ValueError('Unsupported VK media')
        if kind == 'photo':
            server = await self.call('photos.getMessagesUploadServer', peer_id=peer_id)
            field = 'photo'
        else:
            server = await self.call('docs.getMessagesUploadServer', type='audio_message' if kind == 'voice' else 'doc', peer_id=peer_id)
            field = 'file'
        from urllib.parse import urlsplit
        url = urlsplit(str(server.get('upload_url', '')))
        if url.scheme != 'https' or not (url.hostname or '').endswith(('vk.com', 'vk.ru', 'userapi.com')):
            raise VkAPIError(0, 'unexpected upload host')
        try:
            response = await self.raw.post(str(server['upload_url']), files={field: (filename, data, mime_type)})
            uploaded = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            raise VkAPIError(0, 'network') from None
        if kind == 'photo':
            saved = await self.call('photos.saveMessagesPhoto', photo=uploaded.get('photo'),
                                    server=uploaded.get('server'), hash=uploaded.get('hash'))
            item = saved[0]
            key = f"_{item['access_key']}" if item.get('access_key') else ''
            return f"photo{item['owner_id']}_{item['id']}{key}"
        saved = await self.call('docs.save', file=uploaded.get('file'), title=filename[:100])
        if isinstance(saved, dict):
            item = saved.get('audio_message') or saved.get('doc') or saved       # голосовое сообщение возвращается в поле audio_message
        else:
            item = saved[0]
        key = f"_{item['access_key']}" if item.get('access_key') else ''
        return f"doc{item['owner_id']}_{item['id']}{key}"
