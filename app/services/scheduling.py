"""Timezone-aware subscription scheduling."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


def parse_hhmm(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Time must be HH:MM") from exc
    return parsed.replace(second=0, microsecond=0)


def next_occurrence(
    send_time: time,
    timezone_name: str,
    days_of_week: list[int] | tuple[int, ...] | None = None,
    *,
    now: datetime | None = None,
) -> datetime:
    zone = ZoneInfo(validate_timezone(timezone_name))
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    allowed = set(days_of_week or range(1, 8))
    if not allowed or any(day not in range(1, 8) for day in allowed):
        raise ValueError("days_of_week values must be ISO weekdays 1..7")

    for offset in range(0, 14):
        candidate_date: date = current.date() + timedelta(days=offset)
        if candidate_date.isoweekday() not in allowed:
            continue
        candidate = datetime.combine(candidate_date, send_time, tzinfo=zone)
        if candidate > current:
            return candidate.astimezone(timezone.utc)
    raise RuntimeError("Unable to calculate the next schedule occurrence")
