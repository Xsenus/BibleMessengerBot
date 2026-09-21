"""Strict wall-clock schedules: one occurrence per local date, including DST."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(value: str) -> str:
    """Require an IANA timezone, not a host-dependent abbreviation/offset."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f'Unknown IANA timezone: {value}') from exc
    return value


def parse_hhmm(value: str) -> time:
    """Reject seconds, offsets, compact/ambiguous time strings."""
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Time must be HH:MM (00:00..23:59)')
    return time(int(value[:2]), int(value[3:]))


def next_occurrence(send_time: time, timezone_name: str,
                    days_of_week: list[int] | tuple[int, ...] | None = None,
                    *, now: datetime | None = None) -> datetime:
    """Choose the first fold on autumn DST; move a spring gap forward by the gap.

    Compare UTC instants, not two ambiguous local times. A missed first fold is
    not re-sent during the second fold. Dates that do not exist are skipped.
    """
    if send_time.tzinfo is not None or send_time.second or send_time.microsecond:
        raise ValueError('send_time must be a naive HH:MM time')
    zone = ZoneInfo(validate_timezone(timezone_name))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    current_utc = current.astimezone(timezone.utc)
    allowed = set(range(1, 8) if days_of_week is None else days_of_week)
    if not allowed or any(type(day) is not int or not 1 <= day <= 7 for day in allowed):
        raise ValueError('days_of_week values must be ISO weekdays 1..7')
    local_date = current.astimezone(zone).date()
    for offset in range(15):
        day = local_date + timedelta(days=offset)
        if day.isoweekday() not in allowed:
            continue
        requested = datetime.combine(day, send_time, tzinfo=zone).replace(fold=0)
        candidate = requested.astimezone(timezone.utc)
        normalized = candidate.astimezone(zone)
        if normalized.date() != day:
            continue
        if candidate > current_utc:
            return candidate
    raise RuntimeError('Unable to calculate the next schedule occurrence')
