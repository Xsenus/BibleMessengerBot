"""Шифрование секретов, которые администратор вводит в панели (ключи генераторов картинок).

Ключ шифрования берётся из `SECRETS_KEY`, а если он не задан — из `ADMIN_API_KEY` (через SHA-256). Алгоритм — Fernet
(AES-128-CBC + HMAC). При смене ключа шифрования сохранённые секреты не расшифруются: их нужно ввести заново.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    """Fernet из переменных окружения; без достаточно длинного ключа хранить секреты в БД запрещено."""
    material = os.getenv('SECRETS_KEY', '').strip() or os.getenv('ADMIN_API_KEY', '').strip()
    if len(material) < 24:
        raise RuntimeError('SECRETS_KEY or ADMIN_API_KEY (24+ characters) is required to store secrets')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode('utf-8')).digest()))


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
