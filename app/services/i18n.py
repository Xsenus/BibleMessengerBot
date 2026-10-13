"""Тексты интерфейса на языке назначения; отделены от содержимого Библии."""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
from app.catalog.profiles import ISO_639_1_TO_3

ROOT = Path(__file__).resolve().parents[2] / 'locales'
ISO3_TO_UI = {v: k for k, v in ISO_639_1_TO_3.items() if k not in {'iw', 'no'}}
ISO3_TO_UI.update({'nob':'no', 'fil':'tl', 'yue':'zh', 'grc':'el','ara':'ar','fas':'fa','zho':'zh','heb':'he','msa':'ms','swa':'sw'})


@lru_cache(maxsize=1)
def catalogs() -> dict[str, dict[str, str]]:
    """Загрузить и строго проверить все каталоги перевода интерфейса из `locales/` (набор ключей как у английского, без пустых значений). Результат кэшируется."""
    data = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in ROOT.glob('*.json')}
    base = set(data['en'])
    for locale, values in data.items():
        if set(values) != base or not all(isinstance(x, str) and x for x in values.values()):
            raise ValueError(f'Incomplete UI catalog: {locale}')
    return data


def available_ui() -> dict[str, str]:
    """Языки интерфейса, у которых есть полный каталог: {код: родное название}."""
    return {k: v['_name'] for k, v in catalogs().items()}


def ui_for_language(value: str | None) -> str | None:
    """Язык интерфейса для кода языка (`ru`, `rus`, `pt-BR`…) или None, если полного каталога нет; подмена другим языком не делается."""
    if not value:
        return None
    code = value.lower().replace('_','-').split('-')[0]
    code = ISO3_TO_UI.get(code, code)
    return code if code in catalogs() else None


def initial_ui(telegram_language: str | None) -> str:
    """Начальный язык нового личного чата: по языку Telegram, иначе английский."""
    return ui_for_language(telegram_language) or 'en'


def tr(locale: str, key: str, **values: object) -> str:
    """Получить перевод по ключу и подставить значения; неизвестный язык или ключ — ошибка программирования."""
    if locale not in catalogs():
        raise ValueError(f'Unsupported UI locale: {locale}')
    return catalogs()[locale][key].format(**values)


def native_ui_name(locale: str) -> str:
    """Родное название языка интерфейса для списков и настроек."""
    return catalogs()[locale]['_name']
