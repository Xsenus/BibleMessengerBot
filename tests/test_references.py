from __future__ import annotations

import pytest

from app.catalog.references import pair_verses, parse_reference, parse_reference_lines


def test_parse_reference():
    ref = parse_reference("GEN 1:1", 1)
    assert (ref.book_code, ref.chapter, ref.verse, ref.line_number) == ("GEN", 1, 1, 1)


def test_invalid_reference_is_rejected():
    with pytest.raises(ValueError):
        parse_reference("Genesis 1:1", 1)


def test_pair_verses_preserves_missing_and_ranges():
    refs = parse_reference_lines(["GEN 1:1", "GEN 1:2", "GEN 1:3", "GEN 1:4"])
    rows = pair_verses(refs, ["Text", "", "Range text", "<range>"])
    assert rows == [
        ("GEN", 1, 1, "Text", False, 1),
        ("GEN", 1, 3, "Range text", False, 3),
        ("GEN", 1, 4, "", True, 4),
    ]


def test_line_count_must_match():
    refs = parse_reference_lines(["GEN 1:1"])
    with pytest.raises(ValueError, match="line count mismatch"):
        pair_verses(refs, ["a", "b"])
