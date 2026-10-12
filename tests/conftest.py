"""Общие настройки тестов: раздел поддержки проекта по умолчанию включён для прежних тестов
(в бою он выключен SUPPORT_SECTION_ENABLED=false); отдельные тесты проверяют скрытие."""
import pytest


@pytest.fixture(autouse=True)
def _support_section_on(monkeypatch):
    monkeypatch.setenv('SUPPORT_SECTION_ENABLED', 'true')


@pytest.fixture(autouse=True)
def _reset_feature_overrides():
    """Переопределения разделов из БД не должны переходить из одного теста в другой."""
    from app.services import features
    from app.services import image_router
    features._override.clear()
    image_router._CUSTOM.clear()
    yield
    features._override.clear()
    image_router._CUSTOM.clear()
