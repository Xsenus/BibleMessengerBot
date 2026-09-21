from __future__ import annotations

from app.catalog.models import LicenseInfo, TranslationMeta
from app.catalog.policy import decide_license, normalize_license


def meta(license_type: str, *, copyright_notice: str = "", redistributable: bool = True):
    return TranslationMeta(
        language_code="eng",
        translation_id="sample",
        language_name="English",
        language_name_english="English",
        title="Sample",
        description="",
        redistributable=redistributable,
        copyright_notice=copyright_notice,
        publication_url="",
        ot_books=39,
        ot_chapters=929,
        ot_verses=23145,
        nt_books=27,
        nt_chapters=260,
        nt_verses=7957,
        dc_books=0,
        dc_chapters=0,
        dc_verses=0,
        text_direction="ltr",
        downloadable=True,
        short_title="Sample",
        script="Latin",
        source_date=None,
        license=LicenseInfo("sample", license_type),
    )


def test_public_domain_allowed():
    decision = decide_license(meta("public domain"))
    assert decision.allowed


def test_cc_by_and_by_sa_allowed():
    assert decide_license(meta("by", copyright_notice="CC BY 4.0")).allowed
    assert decide_license(meta("by-sa", copyright_notice="Creative Commons BY-SA 4.0")).allowed


def test_nc_and_nd_rejected_by_default():
    assert not decide_license(meta("by-nc-nd")).allowed
    assert not decide_license(meta("by-nd")).allowed


def test_non_redistributable_rejected_even_if_public_domain_label():
    decision = decide_license(meta("public domain", redistributable=False))
    assert not decision.allowed


def test_unknown_requires_explicit_override():
    assert not decide_license(meta("custom permission")).allowed
    assert decide_license(meta("custom permission"), allow_unknown=True).allowed


def test_license_normalization():
    assert "public domain" in normalize_license("PUBLIC_DOMAIN")
