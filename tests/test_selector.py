from __future__ import annotations

from dataclasses import replace

from app.catalog.models import LicenseInfo, TranslationMeta
from app.catalog.selector import select_translations


def edition(identifier: str, language: str, *, ot=39, nt=27, verses=31000, license_type="public domain"):
    return TranslationMeta(
        language_code=language,
        translation_id=identifier,
        language_name=language,
        language_name_english=language,
        title=identifier,
        description="",
        redistributable=True,
        copyright_notice="",
        publication_url="",
        ot_books=ot,
        ot_chapters=0,
        ot_verses=max(0, verses - 8000),
        nt_books=nt,
        nt_chapters=0,
        nt_verses=min(8000, verses),
        dc_books=0,
        dc_chapters=0,
        dc_verses=0,
        text_direction="ltr",
        downloadable=True,
        short_title=identifier,
        script="Latin",
        source_date=None,
        license=LicenseInfo(identifier, license_type),
    )


def test_core_profile_picks_one_per_language_and_prefers_known_id():
    catalog = [
        edition("other", "eng", verses=32000),
        edition("engweb", "eng", verses=31000),
        edition("rus_synodal", "rus"),
    ]
    result = select_translations(catalog, profile="core", max_editions_per_language=1)
    ids = {item.metadata.translation_id for item in result.selected}
    assert "engweb" in ids
    assert "rus_synodal" in ids
    assert "other" not in ids


def test_restricted_is_rejected():
    result = select_translations(
        [edition("restricted", "eng", license_type="by-nc-nd")],
        profile="core",
    )
    assert not result.selected
    assert len(result.rejected) == 1


def test_all_open_keeps_all_allowed_editions():
    result = select_translations(
        [edition("one", "eng"), edition("two", "eng"), edition("tres", "spa")],
        profile="all-open",
        max_editions_per_language=1,
    )
    assert len(result.selected) == 3
