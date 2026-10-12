"""Ротация ключа шифрования секретов и проверка миграций."""
import pytest

from app import selfcheck
from app.services import secrets_box

OLD = 'o' * 32
NEW = 'n' * 32


def test_rotation_keeps_old_secrets_readable(monkeypatch):
    monkeypatch.delenv('SECRETS_KEY', raising=False)
    monkeypatch.delenv('SECRETS_KEY_OLD', raising=False)
    monkeypatch.setenv('ADMIN_API_KEY', OLD)
    token = secrets_box.encrypt('sk-secret')
    monkeypatch.setenv('SECRETS_KEY', NEW)
    assert secrets_box.decrypt(token) == 'sk-secret'          # прежний ключ (ADMIN_API_KEY) продолжает работать
    assert secrets_box.needs_rotation(token)
    fresh = secrets_box.encrypt('sk-secret')
    assert not secrets_box.needs_rotation(fresh)
    monkeypatch.setenv('ADMIN_API_KEY', 'z' * 32)
    monkeypatch.setenv('SECRETS_KEY_OLD', OLD)
    assert secrets_box.decrypt(token) == 'sk-secret'
    monkeypatch.setenv('SECRETS_KEY_OLD', '')
    with pytest.raises(ValueError):
        secrets_box.decrypt(token)


def test_item_statuses():
    assert selfcheck.item('x', None)['status'] == 'skipped'
    assert selfcheck.item('x', True)['status'] == 'ok'
    assert selfcheck.item('x', False)['status'] == 'warn'
    assert selfcheck.item('x', False, critical=True)['status'] == 'fail'
