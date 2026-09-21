from __future__ import annotations

from app.catalog.models import TranslationMeta
from app.catalog.source import BibleNlpSource


def metadata(identifier="abt-maprik", language="abt"):
    return TranslationMeta(
        language_code=language,
        translation_id=identifier,
        language_name="Language",
        language_name_english="Language",
        title="Title",
        description="",
        redistributable=True,
        copyright_notice="",
        publication_url="",
        ot_books=0,
        ot_chapters=0,
        ot_verses=0,
        nt_books=27,
        nt_chapters=260,
        nt_verses=7950,
        dc_books=0,
        dc_chapters=0,
        dc_verses=0,
        text_direction="ltr",
        downloadable=True,
        short_title="Title",
        script="Latin",
        source_date=None,
    )


def test_corpus_candidates_include_hyphen_and_underscore():
    candidates = BibleNlpSource.corpus_candidates(metadata())
    assert "corpus/abt-abt-maprik.txt" in candidates
    assert "corpus/abt-abt_maprik.txt" in candidates


def test_parse_catalog_and_license_join():
    licenses = BibleNlpSource._parse_licenses(
        "ID\tFile\tLanguage\tDialect\tVernacular Title\tLicence Type\tLicence Version\tCC Licence Link\tCopyright Holder\tCopyright Years\tTranslation by\n"
        "abc_test\tfile\tX\t\t\tpublic domain\t\t\tHolder\t1900\tTranslator\n"
    )
    csv_text = (
        'languageCode,translationId,languageName,languageNameInEnglish,dialect,homeDomain,title,description,Redistributable,Copyright,UpdateDate,publicationURL,OTbooks,OTchapters,OTverses,NTbooks,NTchapters,NTverses,DCbooks,DCchapters,DCverses,FCBHID,Certified,inScript,swordName,rodCode,textDirection,downloadable,font,shortTitle,PODISBN,script,sourceDate\n'
        'abc,abc-test,X,X,,,Title,Description,True,Public Domain,2026-01-01,https://example.test,39,929,23145,27,260,7957,0,0,0,,True,,,,ltr,True,,Short,,Latin,2026-01-01\n'
    )
    items = BibleNlpSource._parse_translations(csv_text, licenses)
    assert len(items) == 1
    assert items[0].license is not None
    assert items[0].license.license_type == "public domain"
    assert items[0].coverage == "full"
