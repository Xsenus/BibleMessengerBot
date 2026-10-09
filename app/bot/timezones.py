"""Explicit destination-local city selection; Telegram does not expose a timezone."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.commands import encode_callback

CITIES = [
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Магадан", "Asia/Magadan"),
    ("Камчатка", "Asia/Kamchatka"),
]


def menu(chat):
    ru = chat["ui_language"] == "ru"
    text = (
        "🌍 <b>Выберите свой город или часовой пояс</b>\n\nМолитва приходит в 09:38 и 21:13 по вашему местному времени, чтение — в 09:00 и 21:00. Telegram не сообщает боту ваше местоположение.\n\nДля другого города: <code>/timezone Europe/Berlin</code>"
        if ru
        else "🌍 <b>Choose your city or time zone</b>\n\nPrayer arrives at 09:38 and 21:13 in your local time; reading at 09:00 and 21:00. Telegram does not share your location.\n\nAnother city: <code>/timezone Europe/Berlin</code>"
    )
    buttons = [
        InlineKeyboardButton(
            text=label if ru else zone.rsplit("/", 1)[-1].replace("_", " "),
            callback_data=encode_callback("setzone", chat["telegram_chat_id"], zone),
        )
        for label, zone in CITIES
    ]
    return text, InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )
