"""Шифрование секретов, которые администратор вводит в панели (ключи генераторов картинок).

Ключ шифрования берётся из `SECRETS_KEY`, а если он не задан — из `ADMIN_API_KEY` (через SHA-256). Алгоритм — Fernet
(AES-128-CBC + HMAC). Смена ключа без потерь: новый ключ в `SECRETS_KEY`, прежний — в `SECRETS_KEY_OLD`, затем `python -m app.selfcheck --rotate-secrets`.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _material() -> list[str]:
    """Ключи по убыванию приоритета: основной `SECRETS_KEY`, затем старые из `SECRETS_KEY_OLD` (через запятую) и `ADMIN_API_KEY`
    (им шифровались секреты, пока отдельного ключа не было). Шифрует всегда первый, расшифровать можно любым — так ключ меняют без потерь."""
    primary = os.getenv('SECRETS_KEY', '').strip()
    old = [k.strip() for k in os.getenv('SECRETS_KEY_OLD', '').split(',') if k.strip()]
    admin = os.getenv('ADMIN_API_KEY', '').strip()
    keys = [k for k in [primary, *old, admin] if len(k) >= 24]
    return list(dict.fromkeys(keys))


def _fernet() -> MultiFernet:
    keys = _material()
    if not keys:
        raise RuntimeError('SECRETS_KEY or ADMIN_API_KEY (24+ characters) is required to store secrets')
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode('utf-8')).digest())) for k in keys])


def needs_rotation(token: str) -> bool:
    """True, если секрет зашифрован не основным ключом (его стоит перешифровать)."""
    keys = _material()
    if not token or not keys:
        return False
    try:
        Fernet(base64.urlsafe_b64encode(hashlib.sha256(keys[0].encode('utf-8')).digest())).decrypt(token.encode('ascii'))
        return False
    except InvalidToken:
        return True


def encrypt(value: str) -> str:
    """Зашифровать строку; пустая строка остаётся пустой."""
    return _fernet().encrypt(value.encode('utf-8')).decode('ascii') if value else ''


def decrypt(token: str) -> str:
    """Расшифровать строку; при неверном ключе шифрования — ValueError без раскрытия содержимого."""
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError):
        raise ValueError('secret cannot be decrypted') from None
