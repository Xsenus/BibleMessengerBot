"""Раздел помощи проекту временно скрыт (SUPPORT_SECTION_ENABLED=false) и включается переменной."""
from app.bot import branding, ui
from app.services.features import support_enabled


def test_hidden_by_default_in_production(monkeypatch):
    monkeypatch.delenv('SUPPORT_SECTION_ENABLED')
    assert not support_enabled()
    assert 'donate' not in {c.command for c in branding.commands_for('ru')}
    assert 'paysupport' not in {c.command for c in branding.commands_for('en')}
    labels = [b.text for row in ui.main_keyboard('ru').keyboard for b in row]
    assert not any('Поддержать' in x for x in labels)
    for locale in ('ru', 'en'):
        text = ui.onboarding_help(locale)
        assert '/donate' not in text and '/paysupport' not in text


def test_visible_when_enabled():
    assert support_enabled()
    assert 'donate' in {c.command for c in branding.commands_for('ru')}
    assert any('Поддержать' in b.text for row in ui.main_keyboard('ru').keyboard for b in row)
    assert '/donate' in ui.onboarding_help('ru')
