"""Общие настройки тестов: раздел поддержки проекта по умолчанию включён для прежних тестов
(в бою он выключен SUPPORT_SECTION_ENABLED=false); отдельные тесты проверяют скрытие."""
import pytest


@pytest.fixture(autouse=True)
def _support_section_on(monkeypatch):
    monkeypatch.setenv('SUPPORT_SECTION_ENABLED', 'true')
