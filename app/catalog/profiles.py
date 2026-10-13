"""Профили импорта по языкам и предпочтения изданий."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_profiles(path: Path | None = None) -> dict[str, Any]:
    """Прочитать (и закэшировать) файл `data/language_profiles.json` с профилями и сведениями о языках."""
    resolved = path or Path(__file__).resolve().parents[2] / "data" / "language_profiles.json"
    return json.loads(resolved.read_text(encoding="utf-8"))


def profile_languages(profile: str) -> list[str] | None:
    """Список языков профиля: `None` для `all-open` (все), пустой список для `none`, иначе список из файла."""
    if profile == "all-open":
        return None
    if profile == "none":
        return []
    data = load_profiles()
    return list(data[profile])


def language_details(code: str) -> dict[str, Any]:
    """Сведения о языке из профилей (например, предпочтительные издания); если нет — пустой словарь."""
    return dict(load_profiles().get("languages", {}).get(code, {}))


def preferred_ids(code: str) -> list[str]:
    """Идентификаторы предпочтительных изданий языка в нижнем регистре, по убыванию предпочтения."""
    return [item.lower() for item in language_details(code).get("preferred_ids", [])]


ISO_639_1_TO_3 = {
    "en": "eng", "ru": "rus", "uk": "ukr", "es": "spa", "fr": "fra",
    "de": "deu", "pt": "por", "it": "ita", "nl": "nld", "pl": "pol",
    "cs": "ces", "sk": "slk", "hu": "hun", "ro": "ron", "bg": "bul",
    "sr": "srp", "hr": "hrv", "sl": "slv", "sv": "swe", "no": "nob",
    "da": "dan", "fi": "fin", "el": "ell", "he": "hbo", "iw": "hbo",
    "ar": "arb", "fa": "pes", "tr": "tur", "hi": "hin", "bn": "ben",
    "ur": "urd", "pa": "pan", "gu": "guj", "mr": "mar", "ta": "tam",
    "te": "tel", "ml": "mal", "kn": "kan", "th": "tha", "vi": "vie",
    "id": "ind", "ms": "zlm", "zh": "cmn", "ja": "jpn", "ko": "kor",
    "sw": "swh", "am": "amh", "so": "som", "ha": "hau", "yo": "yor",
    "ig": "ibo", "zu": "zul", "af": "afr", "tl": "tgl", "la": "lat",
}


def normalize_language_code(value: str | None) -> str | None:
    """Привести код языка (`ru`, `en-US`, `pt_BR`) к трёхбуквенному коду ISO 639-3, принятому в каталоге."""
    if not value:
        return None
    code = value.split("-")[0].split("_")[0].lower()
    return ISO_639_1_TO_3.get(code, code)
