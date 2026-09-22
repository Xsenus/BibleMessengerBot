"""Explicit destination-localized interface strings, separate from Bible content."""
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
    """Load and strictly validate all bundled UI catalogs once."""
    data = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in ROOT.glob('*.json')}
    base = set(data['en'])
    for locale, values in data.items():
        if set(values) != base or not all(isinstance(x, str) and x for x in values.values()):
            raise ValueError(f'Incomplete UI catalog: {locale}')
    return data


def available_ui() -> dict[str, str]:
    """Only advertise fully populated local catalogs, never the import wish list."""
    return {k: v['_name'] for k, v in catalogs().items()}


def ui_for_language(value: str | None) -> str | None:
    """Return an available UI locale or None; no silent language substitution."""
    if not value:
        return None
    code = value.lower().replace('_','-').split('-')[0]
    code = ISO3_TO_UI.get(code, code)
    return code if code in catalogs() else None


def initial_ui(telegram_language: str | None) -> str:
    """A new private destination uses Telegram's UI language, otherwise English."""
    return ui_for_language(telegram_language) or 'en'


def tr(locale: str, key: str, **values: object) -> str:
    """Resolve a known translation; missing keys are programming errors."""
    if locale not in catalogs():
        raise ValueError(f'Unsupported UI locale: {locale}')
    return catalogs()[locale][key].format(**values)


def native_ui_name(locale: str) -> str:
    """Display the native language name in selectors and settings."""
    return catalogs()[locale]['_name']
