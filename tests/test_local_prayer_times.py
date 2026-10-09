from datetime import date, time
from zoneinfo import ZoneInfo

from app.bot.commands import decode_callback, parse_command
from app.bot.timezones import menu
from app.services.prayers import invitation
from app.services.scheduling import on_date


def test_same_prayer_wall_time_is_different_utc_in_moscow_and_novosibirsk():
    day = date(2026, 10, 9)
    for clock in (time(9, 38), time(21, 13)):
        moscow = on_date(day, clock, "Europe/Moscow")
        novosibirsk = on_date(day, clock, "Asia/Novosibirsk")
        assert (moscow - novosibirsk).total_seconds() == 4 * 3600
        assert moscow.astimezone(ZoneInfo("Europe/Moscow")).time().replace(tzinfo=None) == clock
        assert (
            novosibirsk.astimezone(ZoneInfo("Asia/Novosibirsk")).time().replace(tzinfo=None)
            == clock
        )


def test_city_picker_carries_authorized_destination_and_zone():
    _text, keyboard = menu(dict(ui_language="ru", telegram_chat_id=101))
    choices = [
        decode_callback(button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert ("setzone", 101, "Europe/Moscow") in choices
    assert ("setzone", 101, "Asia/Novosibirsk") in choices
    assert parse_command("/timezone Europe/Moscow -1001234").target == "-1001234"


def test_prayer_does_not_show_clock_or_iana_identifier():
    result = invitation(
        dict(prayer_text="Господи, услышь нас.", generator="template", news_snapshot=[]),
        "ru",
        "09:38 · Asia/Novosibirsk",
    )
    assert "09:38" not in result and "Asia/" not in result
