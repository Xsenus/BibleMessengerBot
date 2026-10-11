"""Строгие расписания по местному времени: одно событие на местную дату, с учётом перехода на летнее время."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def reading_time(mode: str, clock: time) -> time:
    """Время начала чтения: для утра и вечера заданное время — это молитва, а чтение начинается в начале часа; для остальных режимов — заданное время."""
    return clock.replace(minute=0) if mode in {'morning_verse','evening_verse'} else clock


def next_reading(mode: str, clock: time, timezone_name: str, days=None, *, now=None):
    """Ближайший момент чтения (UTC) для режима, времени, часового пояса и дней недели."""
    return next_occurrence(reading_time(mode,clock),timezone_name,days,now=now)


def on_date(day, clock: time, timezone_name: str) -> datetime:
    """Момент заданного времени на конкретную местную дату по тем же правилам перехода на летнее время, что у планировщика; если такой даты в поясе нет — ошибка."""
    start=datetime.combine(day,time(),tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)
    candidate=next_occurrence(clock,timezone_name,now=start-timedelta(microseconds=1))
    if candidate.astimezone(ZoneInfo(timezone_name)).date()!=day:
        raise ValueError('Schedule date does not exist in this timezone')
    return candidate


def validate_timezone(value: str) -> str:
    """Проверить, что это название часового пояса IANA, а не аббревиатура или смещение."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f'Unknown IANA timezone: {value}') from exc
    return value


def parse_hhmm(value: str) -> time:
    """Разобрать время строго в формате ЧЧ:ММ (без секунд, смещений и сокращённых записей)."""
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Time must be HH:MM (00:00..23:59)')
    return time(int(value[:2]), int(value[3:]))


def next_occurrence(send_time: time, timezone_name: str,
                    days_of_week: list[int] | tuple[int, ...] | None = None,
                    *, now: datetime | None = None) -> datetime:
    """Ближайшее наступление времени в поясе после `now` (UTC) с учётом дней недели. При переводе часов осенью берётся первое вхождение, весной пропущенный час сдвигается вперёд; несуществующие даты пропускаются."""
    if send_time.tzinfo is not None or send_time.second or send_time.microsecond:
        raise ValueError('send_time must be a naive HH:MM time')
    zone = ZoneInfo(validate_timezone(timezone_name))
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    current_utc = current.astimezone(UTC)
    allowed = set(range(1, 8) if days_of_week is None else days_of_week)
    if not allowed or any(type(day) is not int or not 1 <= day <= 7 for day in allowed):
        raise ValueError('days_of_week values must be ISO weekdays 1..7')
    local_date = current.astimezone(zone).date()
    for offset in range(15):
        day = local_date + timedelta(days=offset)
        if day.isoweekday() not in allowed:
            continue
        requested = datetime.combine(day, send_time, tzinfo=zone).replace(fold=0)
        candidate = requested.astimezone(UTC)
        normalized = candidate.astimezone(zone)
        if normalized.date() != day:
            continue
        if candidate > current_utc:
            return candidate
    raise RuntimeError('Unable to calculate the next schedule occurrence')
