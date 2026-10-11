"""Компактная навигация личного чата (нижняя клавиатура) и вводные тексты: приветствие, подсказка меню, справка."""
# ruff: noqa: RUF001  # Russian interface text intentionally uses Cyrillic characters.
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, KeyboardButton, ReplyKeyboardMarkup

from app.services.formatting import escape, plain_text, utf16_length
from app.services.features import support_enabled
from app.services.platforms import is_bridged
from app.services.i18n import available_ui, tr
from app.services.rate_limit import wait_send_slot

LOGGER = logging.getLogger(__name__)
WELCOME_PATH = Path(__file__).resolve().parents[2] / 'assets' / 'brand' / 'welcome.png'


_BASE_BUTTONS = (
    ('next', '📖'), ('today', '🌅'), ('random', '🎲'),
    ('search', '🔎'), ('settings', '⚙️'), ('help', '❔'),
    ('daily', '🔔'),
)
BUTTONS_WITH_SUPPORT = (*_BASE_BUTTONS, ('donate', '⭐'))


def active_buttons():
    """Кнопки главной клавиатуры: «Поддержать» показывается только при включённом разделе поддержки."""
    return BUTTONS_WITH_SUPPORT if support_enabled() else _BASE_BUTTONS


BUTTONS = BUTTONS_WITH_SUPPORT  # все возможные кнопки (для распознавания подписей)
LABELS = {
    'ru': {'next': 'Читать дальше', 'today': 'Стих дня', 'help': 'Помощь'},
    'en': {'next': 'Continue reading', 'today': 'Verse of the day', 'help': 'Help'},
}


SUPPORT_RU = (
    '<b>Добровольная поддержка</b>\n'
    '/donate — поддержать сервер и развитие Telegram Stars\n'
    '/donations — история поддержки · /terms — условия\n'
    '/paysupport текст — вопрос по платежу или запрос возврата\n'
    'Библия бесплатна; поддержка разовая и добровольная.\n\n'
)
SUPPORT_EN = (
    '<b>Voluntary support</b>\n'
    '/donate — support hosting and development with Telegram Stars\n'
    '/donations — support history · /terms — terms\n'
    '/paysupport your message — payment issue or refund request\n'
    'The Bible is free; support is optional and one-time.\n\n'
)


def button_label(locale: str, command: str, emoji: str) -> str:
    """Подпись кнопки нижней клавиатуры: эмодзи + название команды на языке пользователя."""
    if command == 'hide_keyboard':
        return f"{emoji} " + ('Скрыть кнопки' if locale == 'ru' else 'Hide buttons')
    if command == 'donate':
        return f"{emoji} " + ('Поддержать' if locale == 'ru' else 'Support')
    if command == 'daily':
        return f"{emoji} " + ('Стих каждый день' if locale == 'ru' else 'Daily verse')
    return f"{emoji} {LABELS.get(locale, {}).get(command, tr(locale, command))}"


def main_keyboard(locale: str, *, collapsible=True) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура главных действий по две кнопки в ряд; клиент может её свернуть, а при `collapsible` есть кнопка «Скрыть кнопки»."""
    buttons = [KeyboardButton(text=button_label(locale, command, emoji))
               for command, emoji in active_buttons()]
    placeholder = {'ru': 'Выберите действие или введите /search …',
                   'en': 'Choose an action or enter /search …'}.get(locale, tr(locale, 'choose'))
    return ReplyKeyboardMarkup(
        keyboard=[buttons[index:index + 2] for index in range(0, len(buttons), 2)]
                 + ([[KeyboardButton(text=button_label(locale, 'hide_keyboard', '⌨️'))]] if collapsible else []),
        resize_keyboard=True, is_persistent=False, one_time_keyboard=True,
        input_field_placeholder=placeholder,
    )


@lru_cache(maxsize=1)
def _button_commands() -> dict[str, str]:
    """Словарь «подпись кнопки → команда» для всех языков интерфейса (кэшируется)."""
    return {button_label(locale, command, emoji): '/' + command
            for locale in available_ui() for command, emoji in (*BUTTONS, ('hide_keyboard', '⌨️'))}


def keyboard_command(text: str) -> str | None:
    """Вернуть команду для нажатой кнопки, если текст в точности совпал с известной подписью; произвольный текст в команды не превращается."""
    return _button_commands().get(text)


def welcome_text(locale: str, edition_title: str | None = None) -> str:
    """Текст приветствия на языке пользователя; в конце — название выбранного издания или сообщение, что издание ещё не готово."""
    if locale == 'ru':
        text = (
            '📖 <b>Библия — рядом каждый день</b>\n\n'
            'Читайте в своём темпе, находите нужные слова и возвращайтесь к чтению.\n\n'
            '📖 <b>Читать дальше</b> — следующая глава с сохранением прогресса.\n'
            '🌅 <b>Стих дня</b> — отрывок на сегодня по нажатию.\n'
            '🔔 <b>Стих каждый день</b> — включить ежедневную доставку.\n'
            '⚙️ <b>Настройки</b> — перевод, язык и ежедневная рассылка.\n\n'
            'Выберите действие на клавиатуре внизу ↓'
        )
    elif locale == 'en':
        text = (
            '📖 <b>A little time for the Bible, every day</b>\n\n'
            'Read at your own pace, find a passage, and continue where you left off.\n\n'
            '📖 <b>Continue reading</b> — the next chapter, with progress saved.\n'
            '🌅 <b>Verse of the day</b> — today’s passage on request.\n'
            '🔔 <b>Daily verse</b> — turn on daily delivery.\n'
            '⚙️ <b>Settings</b> — edition, language, and daily deliveries.\n\n'
            'Choose an action on the keyboard below ↓'
        )
    else:
        text = f"📖 <b>{escape(tr(locale, 'start'))}</b>\n\n{menu_hint(locale)}"
    if edition_title:
        title = edition_title if len(edition_title) <= 120 else edition_title[:117] + '…'
        text += f"\n\n<i>{escape(tr(locale, 'edition'))}: {escape(title)}</i>"
    else:
        text += '\n\n' + escape(tr(locale, 'not_ready'))
    return text


@lru_cache(maxsize=4)
def _image_digest(path: Path, size: int, modified_ns: int) -> str:
    """Хеш файла картинки приветствия (кэшируется по пути, размеру и времени изменения): заменённая картинка получает новый ключ кэша."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def send_welcome(bot: Any, connection: Any, settings: Any, chat_id: int,
                       text: str, markup: ReplyKeyboardMarkup,
                       *, image_path: Path | None = None) -> bool:
    """Отправить приветствие одним фото (повторно используется file_id Telegram). Возвращает False, если нужно отправить обычный текст: подпись длиннее лимита, нет файла или фото не отправилось."""
    if is_bridged(getattr(bot,'platform',None)):
        await bot.queue_welcome(connection,settings,chat_id,text,markup)
        return True
    path = image_path or WELCOME_PATH
    try:
        if utf16_length(plain_text(text)) > 1024:
            return False
        info = path.stat()
        digest = _image_digest(path, info.st_size, info.st_mtime_ns)
    except (OSError, ValueError):
        return False
    key = f'welcome:{bot.id}:{digest}'
    cached = await connection.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
    await wait_send_slot(connection, chat_id, settings.telegram_global_rate_per_second,
                         settings.telegram_chat_rate_per_second)
    try:
        sent = await bot.send_photo(chat_id=chat_id, photo=cached or FSInputFile(path),
            caption=text, parse_mode='HTML', reply_markup=markup, request_timeout=15)
    except (TelegramAPIError, OSError, TimeoutError) as error:
        if cached and isinstance(error, TelegramBadRequest):
            await connection.execute('DELETE FROM app_settings WHERE key=$1', key)
        LOGGER.warning('Welcome photo unavailable: %s', type(error).__name__)
        return False
    photos = getattr(sent, 'photo', None)
    if photos and not cached:
        try:
            await connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',
                key, photos[-1].file_id)
        except Exception as error:
            # The photo is already delivered; a failed optional cache must not duplicate it.
            LOGGER.warning('Welcome photo cache unavailable: %s', type(error).__name__)
    return True


def menu_hint(locale: str) -> str:
    """Короткая подсказка «с чего начать» после нажатия неизвестной кнопки или текста."""
    if locale == 'ru':
        return ('📖 <b>С чего начнём?</b>\n\nВыберите действие на клавиатуре внизу. '
                'Чтобы найти слова в Библии, отправьте <code>/search любовь</code>.\n\n'
                'Все возможности: /help · Перевод и рассылка: /settings')
    if locale == 'en':
        return ('📖 <b>What would you like to read?</b>\n\nChoose an action on the keyboard below. '
                'To find words in the Bible, send <code>/search love</code>.\n\n'
                'All commands: /help · Edition and deliveries: /settings')
    return (f"{escape(tr(locale, 'choose'))}: /next · /today · /random\n\n"
            f"{escape(tr(locale, 'search'))}: <code>/search …</code>\n"
            f"{escape(tr(locale, 'settings'))}: /settings · {escape(tr(locale, 'help'))}: /help")


def search_prompt(locale: str) -> str:
    """Подсказка по поиску и чтению по адресу (примеры «Иоанна 3:16», `/search любовь`)."""
    if locale == 'ru':
        return ('🔎 <b>Найти слова в Библии</b>\n\n'
            'Отправьте адрес главы или стиха:\n'
            '<code>Иоанна 3</code>\n<code>Иоанна 3:16</code>\n<code>Пс 118:55–57</code>\n\n'
            'Поиск слова или фразы: <code>/search любовь</code>\n\n'
                'Поиск идёт по выбранному переводу. Изменить его: /settings')
    if locale == 'en':
        return ('🔎 <b>Find words in the Bible</b>\n\n'
            'Send a chapter or verse address:\n'
            '<code>John 3</code>\n<code>John 3:16</code>\n<code>Psalm 119:55–57</code>\n\n'
            'Search words: <code>/search love</code>\n\n'
                'Search uses your selected edition. Change it in /settings')
    return (f"🔎 <b>{escape(tr(locale, 'search'))}</b>\n\n<code>/search …</code>\n\n"
            f"{escape(tr(locale, 'edition'))}: /settings")


def onboarding_help(locale: str) -> str | None:
    """Подробная справка по командам для русского и английского; для других языков None (используется общий список)."""
    if locale == 'ru':
        return (
            '❔ <b>Ваше чтение Библии</b>\n\n'
            '<b>Читать сейчас</b>\n'
            '/next — следующая глава; прогресс сохраняется после доставки\n'
            '/today — стих дня · /random — случайный стих\n'
            '/search любовь — поиск по выбранному переводу\n\n'
            '/read Иоанна 3:16 — стих; Иоанна 3 — глава\n\n'
            '<b>Выбрать перевод</b>\n'
            '/settings — язык, перевод и режим чтения\n'
            '/translations — доступные издания · /license — источник текста\n\n'
            '<b>Читать каждый день</b>\n'
            '/daily — включить стих каждый день · /daily off — отключить\n'
            '<code>/subscribe verse 09:00 Europe/Moscow</code>\n'
            '<code>/subscribe reading_plan 09:00 Europe/Moscow bible-365</code>\n'
            '/time — время рассылки · /status — прогресс\n'
            '/pause — пауза · /resume — продолжить · /unsubscribe — отписаться\n\n'
            '<b>Добавить канал или группу</b>\n'
            'Назначьте бота администратором и в личном чате отправьте '
            '<code>/settings @channel</code>. Настройки каждого чата независимы.\n\n'
            '<b>Дополнительно</b>\n'
            '<code>/language ru</code> · <code>/ui en</code>\n'
            '<code>/thread 123 @group</code> — тема форума\n'
            '<code>/reset confirm</code> — сбросить прогресс и приостановить подписки\n'
            '/topics — темы для изданий с совместимой нумерацией\n\n'
            + (SUPPORT_RU if support_enabled() else '') +
            'Скрыть кнопки: /hide_keyboard. Вернуть: /menu. Голос чтения: /voice'
        )
    if locale == 'en':
        return (
            '❔ <b>Your Bible reading</b>\n\n'
            '<b>Read now</b>\n'
            '/next — the next chapter; progress is saved after delivery\n'
            '/today — verse of the day · /random — random verse\n'
            '/search love — search your selected edition\n\n'
            '/read John 3:16 — a verse; John 3 — a chapter\n\n'
            '<b>Choose an edition</b>\n'
            '/settings — language, edition, and reading mode\n'
            '/translations — available editions · /license — text source\n\n'
            '<b>Read every day</b>\n'
            '/daily — enable daily verses · /daily off — disable\n'
            '<code>/subscribe verse 09:00 Europe/London</code>\n'
            '<code>/subscribe reading_plan 09:00 Europe/London bible-365</code>\n'
            '/time — delivery time · /status — progress\n'
            '/pause — pause · /resume — continue · /unsubscribe — stop deliveries\n\n'
            '<b>Add a channel or group</b>\n'
            'Make the bot an administrator, then send <code>/settings @channel</code> '
            'in this private chat. Each destination has its own settings.\n\n'
            '<b>More options</b>\n'
            '<code>/language en</code> · <code>/ui en</code>\n'
            '<code>/thread 123 @group</code> — choose a forum topic\n'
            '<code>/reset confirm</code> — reset progress and pause subscriptions\n'
            '/topics — themes for editions with compatible numbering\n\n'
            + (SUPPORT_EN if support_enabled() else '') +
            'Hide buttons: /hide_keyboard. Restore buttons: /menu. Russian voices: /voice'
        )
    return None
