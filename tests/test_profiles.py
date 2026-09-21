from app.catalog.profiles import language_details, normalize_language_code, profile_languages


def test_profiles_have_expected_languages():
    core = profile_languages("core")
    extended = profile_languages("extended")
    assert core is not None and extended is not None
    assert {"eng", "rus", "ukr", "spa", "fra", "cmn", "arb"}.issubset(core)
    assert set(core).issubset(extended)
    assert len(extended) >= 50


def test_rtl_language_metadata():
    assert language_details("arb")["direction"] == "rtl"


def test_iso_language_normalization():
    assert normalize_language_code("ru-RU") == "rus"
    assert normalize_language_code("es") == "spa"
    assert normalize_language_code("cmn") == "cmn"


def test_verified_upstream_preferences_are_present():
    assert language_details("rus")["preferred_ids"][0] == "russyn"
    assert "eng-web" in language_details("eng")["preferred_ids"]
    assert "spaRV1909" in language_details("spa")["preferred_ids"]
