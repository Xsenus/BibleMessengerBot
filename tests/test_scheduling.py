from datetime import datetime, time, timezone

import pytest

from app.services.scheduling import next_occurrence, parse_hhmm, validate_timezone


def test_parse_hhmm():
    assert parse_hhmm("09:30") == time(9, 30)


def test_invalid_time():
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_next_occurrence_same_day():
    now = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    result = next_occurrence(time(9, 0), "UTC", now=now)
    assert result == datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


def test_next_occurrence_respects_weekdays():
    # 2026-09-21 is Monday; only Tuesday is allowed.
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    result = next_occurrence(time(9, 0), "UTC", [2], now=now)
    assert result == datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)


def test_timezone_validation():
    assert validate_timezone("Europe/Amsterdam") == "Europe/Amsterdam"
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus")
