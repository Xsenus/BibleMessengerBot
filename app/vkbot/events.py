"""Приём событий Long Poll ВКонтакте: проверка формы, ключ дедупликации и надёжная запись во входящую очередь."""
from __future__ import annotations

import hashlib
import json
from typing import Any

UPDATE_TYPES = ('message_new', 'message_event', 'message_allow', 'message_deny', 'wall_post_new')
MAX_EVENT_BYTES = 262144


def parse_event(update: Any) -> tuple[str, str, dict]:
    """Проверить событие Long Poll и вернуть (ключ дедупликации, ключ очереди чата, событие).

    Ключ дедупликации — SHA-256 от типа и `event_id` (уникален для события); очередь чата упорядочивает события одной беседы.
    """
    if not isinstance(update, dict) or not isinstance(update.get('type'), str) or not isinstance(update.get('object'), dict):
        raise ValueError('Invalid VK event')
    if len(json.dumps(update, ensure_ascii=False)) > MAX_EVENT_BYTES:
        raise ValueError('VK event exceeds size limit')
    kind, obj = update['type'], update['object']
    event_id = update.get('event_id')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise ValueError('Invalid VK event id')
    message = obj.get('message') if isinstance(obj.get('message'), dict) else obj
    peer = message.get('peer_id') or obj.get('peer_id') or obj.get('user_id') or message.get('from_id') or obj.get('owner_id')
    if type(peer) is not int:
        raise ValueError('Invalid VK peer')
    key = hashlib.sha256(f'{kind}:{event_id}'.encode()).hexdigest()
    return key, str(peer), update


async def ingest(connection: Any, update: Any) -> bool:
    """Сохранить событие во входящую очередь `vk_inbox`; повтор того же события нового задания не создаёт.
    Возвращает True только для нового события."""
    key, chat_key, event = parse_event(update)
    result = await connection.fetchval('''INSERT INTO vk_inbox(event_key,payload,chat_key) VALUES($1,$2::jsonb,$3)
        ON CONFLICT(event_key) DO NOTHING RETURNING id''', key, json.dumps(event, ensure_ascii=False), chat_key)
    return result is not None
