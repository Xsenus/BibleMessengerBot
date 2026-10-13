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


@pytest.fixture(autouse=True)
def _no_dns_checks(monkeypatch):
    """В тестах имена хостов вымышленные: проверка разрешения DNS отключена (её поведение проверяется отдельным тестом)."""
    from app.services import chat_sources
    monkeypatch.setattr(chat_sources, 'DNS_CHECK', False)


@pytest.fixture(autouse=True)
def _ocr_stub_when_tesseract_missing(request, monkeypatch):
    """Без установленного tesseract (в образе приложения и в CI он есть) проверка надписей на картинках считалась бы недоступной,
    и тесты маршрутизации генераторов падали бы по постороннему поводу. Подставляем «одобрено»; сама проверка тестируется в test_image_quality*."""
    import shutil
    if shutil.which('tesseract') or request.node.module.__name__.rsplit('.', 1)[-1].startswith('test_image_quality'):
        return
    from app.services import image_quality

    async def approved(data, *args, **kwargs):
        return dict(verdict='approved', reason='ocr_stub', metrics={})
    monkeypatch.setattr(image_quality, 'assess', approved)
