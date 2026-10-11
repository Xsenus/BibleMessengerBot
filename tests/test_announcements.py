"""Объявления: разбор расписаний, вычисление следующего запуска, повторы и ограничения."""
from datetime import UTC, datetime, time, timedelta

import pytest

from app.services import announcements as a
from app.services.errors import UserError

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)          # среда


def row(**kw):
    base = dict(kind='daily', send_time=time(9), timezone='Europe/Moscow', days_of_week=[1, 2, 3, 4, 5, 6, 7],
                day_of_month=None, interval_minutes=None, start_at=None, end_at=None, max_runs=None, runs_done=0)
    return {**base, **kw}


def test_parse_weekly_saturday_with_options():
    s = a.parse_when('weekly sat 10:00 Europe/Moscow x4 until 2026-12-31', 'UTC', NOW)
    assert s['kind'] == 'weekly' and s['days_of_week'] == [6] and s['send_time'] == time(10)
    assert s['timezone'] == 'Europe/Moscow' and s['max_runs'] == 4 and s['end_at'].year == 2026


@pytest.mark.parametrize('text,kind', [('now', 'once'), ('сейчас', 'once'), ('daily 09:00', 'daily'), ('ежедневно 21:30', 'daily'),
                                       ('monthly 15 10:00', 'monthly'), ('every 90m', 'interval'), ('every 2h', 'interval'),
                                       ('weekly weekdays 08:30', 'weekly'), ('once 2026-10-20 18:00', 'once')])
def test_parse_kinds(text, kind):
    assert a.parse_when(text, 'Europe/Moscow', NOW)['kind'] == kind


@pytest.mark.parametrize('text', ['', 'whenever', 'daily 25:00', 'weekly xyz 10:00', 'monthly 40 10:00', 'every 1m', 'once 2026-13-01 10:00'])
def test_parse_rejects_garbage(text):
    with pytest.raises(UserError):
        a.parse_when(text, 'UTC', NOW)


def test_weekly_saturday_next_and_following():
    r = row(kind='weekly', days_of_week=[6], send_time=time(10))
    first = a.compute_next(r, None, NOW)
    assert first == datetime(2026, 10, 17, 7, 0, tzinfo=UTC)                    # суббота 10:00 по Москве
    assert a.compute_next(r, first, first + timedelta(seconds=5)) == first + timedelta(days=7)


def test_daily_never_repeats_same_instant_and_skips_missed():
    r = row()
    first = a.compute_next(r, None, NOW)
    assert first > NOW
    later = a.compute_next(r, first, first + timedelta(days=3, minutes=1))       # сервис простоял — пропущенное не догоняется
    assert later > first + timedelta(days=3)


def test_monthly_clamps_to_last_day():
    r = row(kind='monthly', day_of_month=31, send_time=time(10), timezone='UTC')
    nxt = a.compute_next(r, None, datetime(2026, 2, 10, tzinfo=UTC))
    assert (nxt.month, nxt.day) == (2, 28)


def test_interval_and_end_limit():
    r = row(kind='interval', interval_minutes=90)
    assert a.compute_next(r, None, NOW) == NOW + timedelta(minutes=90)
    prev = NOW + timedelta(minutes=90)
    assert a.compute_next(r, prev, prev + timedelta(minutes=1)) == prev + timedelta(minutes=90)
    assert a.compute_next(row(kind='interval', interval_minutes=90, end_at=NOW + timedelta(minutes=10)), None, NOW) is None


def test_once_has_no_repeat():
    r = row(kind='once', start_at=NOW + timedelta(hours=1))
    assert a.compute_next(r, None, NOW) == NOW + timedelta(hours=1)
    assert a.compute_next(r, NOW + timedelta(hours=1), NOW + timedelta(hours=2)) is None


def test_render_escapes_markup():
    assert a.render('A <b>', 'x < y & z') == '<b>A &lt;b&gt;</b>\n\nx &lt; y &amp; z'
    assert a.render('', 'text') == 'text'
