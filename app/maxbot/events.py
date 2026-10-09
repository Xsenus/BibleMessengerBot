"""Bounded, authenticated webhook ingestion with durable deduplication."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from app.maxbot.identities import valid_external_id

MAX_EVENT_BYTES = 65536
UPDATE_TYPES = ('message_created', 'message_callback', 'bot_started', 'bot_stopped',
                'bot_added', 'bot_removed', 'dialog_removed', 'chat_title_changed',
                'bot_admin_permissions_changed')


def authorized(expected: str, supplied: str | None) -> bool:
    return bool(expected and supplied and len(expected) >= 24
                and secrets.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')))


def parse_event(raw: bytes) -> tuple[str, dict]:
    if not raw or len(raw) > MAX_EVENT_BYTES:
        raise ValueError('MAX event exceeds size limit')
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('Invalid MAX event JSON') from None
    if not isinstance(event, dict):
        raise ValueError('Invalid MAX event')
    kind = event.get('update_type')
    if not isinstance(kind, str) or not re.fullmatch(r'[a-z_]{1,64}', kind):
        raise ValueError('Invalid MAX event type')
    timestamp = event.get('timestamp')
    if type(timestamp) is not int or not 0 <= timestamp < 2**63:
        raise ValueError('Invalid MAX event timestamp')
    identity = None
    if kind == 'message_created':
        message = event.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('body'), dict):
            raise ValueError('Invalid MAX message')
        mid = message['body'].get('mid')
        text = message['body'].get('text')
        if not isinstance(mid, str) or not 1 <= len(mid) <= 256:
            raise ValueError('Invalid MAX message ID')
        if text is not None and (not isinstance(text, str) or len(text) > 4000):
            raise ValueError('Invalid MAX message text')
        recipient = message.get('recipient')
        if not isinstance(recipient, dict) or recipient.get('chat_type') not in {'dialog', 'chat', 'channel'}:
            raise ValueError('Invalid MAX recipient')
        valid_external_id(recipient.get('chat_id'))
        sender = message.get('sender')
        if sender is not None:
            if not isinstance(sender, dict):
                raise ValueError('Invalid MAX sender')
            valid_external_id(sender.get('user_id'))
        identity = mid
    elif kind == 'message_callback':
        callback = event.get('callback')
        if not isinstance(callback, dict):
            raise ValueError('Invalid MAX callback')
        identity = callback.get('callback_id')
        payload = callback.get('payload')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
            raise ValueError('Invalid MAX callback ID')
        if payload is not None and (not isinstance(payload, str) or len(payload.encode()) > 1024):
            raise ValueError('Invalid MAX callback payload')
        actor = callback.get('user')
        if not isinstance(actor, dict):
            raise ValueError('Invalid MAX callback user')
        valid_external_id(actor.get('user_id'))
    elif kind in {'bot_started', 'bot_stopped', 'bot_added', 'bot_removed',
                  'dialog_removed', 'chat_title_changed', 'bot_admin_permissions_changed'}:
        valid_external_id(event.get('chat_id'))
        if kind in {'bot_started', 'bot_stopped', 'bot_added', 'bot_removed'}:
            user = event.get('user')
            if not isinstance(user, dict):
                raise ValueError('Invalid MAX event user')
            valid_external_id(user.get('user_id'))
    # Unknown, well-formed future event types can be acknowledged and marked ignored.
    stable = kind + ':' + identity if identity is not None else json.dumps(event, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(stable.encode('utf-8')).hexdigest(), event


async def ingest(connection: Any, raw: bytes) -> bool:
    """True only for a newly committed event; a repeated webhook creates no new job."""
    key, event = parse_event(raw)
    result = await connection.fetchval('''INSERT INTO max_inbox(event_key,payload)
        VALUES($1,$2::jsonb) ON CONFLICT(event_key) DO NOTHING RETURNING id''',
        key, json.dumps(event, ensure_ascii=False))
    return result is not None
