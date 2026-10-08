"""Private image credentials, deduplicated without exposing their values."""
from __future__ import annotations

import json
import os
import re

MAX_KEYS = 32


def image_keys(primary='', additional=()):
    keys = tuple(dict.fromkeys(key.strip() for key in (primary, *additional) if key.strip()))
    if len(keys) > MAX_KEYS or any(len(key) > 512 or not re.fullmatch(r'[A-Za-z0-9_-]+', key) for key in keys):
        raise ValueError('Invalid OpenAI image credentials')
    return keys


def from_env():
    raw = os.getenv('OPENAI_API_KEYS', '').strip()
    try:
        additional = json.loads(raw) if raw.startswith('[') else raw.replace('\n', ',').split(',')
        if not isinstance(additional, list) or any(not isinstance(key, str) for key in additional):
            raise ValueError()
        return image_keys(os.getenv('OPENAI_API_KEY', ''), additional)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid OpenAI image credentials') from None
