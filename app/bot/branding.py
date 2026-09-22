"""Idempotent public Telegram profile and menu, independent of polling health."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import BotCommand, FSInputFile, InputProfilePhotoStatic, MenuButtonCommands

from app.services.i18n import available_ui, tr

LOGGER = logging.getLogger(__name__)
AVATAR_PATH = Path(__file__).resolve().parents[2] / 'assets' / 'brand' / 'avatar.png'

PROFILE = {
    'ru': {
        'name': 'Библия каждый день',
        'short_description': 'Стих дня, чтение Библии по главам и планы чтения. Лично, в группах и каналах.',
        'description': '📖 Библия каждый день\n\n'
            'Читайте Синодальный перевод и World English Bible: стих дня, следующая глава, поиск и планы чтения.\n\n'
            'Выберите удобное время и получайте чтение в личном чате, группе или канале. '
            'Прогресс сохраняется, рассылку можно поставить на паузу.\n\n'
            'Нажмите «Начать», чтобы выбрать перевод и настроить чтение.',
    },
    'en': {
        'name': 'Bible Every Day',
        'short_description': 'Daily verses, chapter reading and Bible plans. Read privately or share with your group or channel.',
        'description': '📖 Bible Every Day\n\n'
            'Read the World English Bible and Russian Synodal Bible: daily verses, the next chapter, search and reading plans.\n\n'
            'Choose your schedule for a private chat, group or channel. '
            'Your reading progress is saved, and you can pause deliveries anytime.\n\n'
            'Tap Start to choose a translation and begin reading.',
    },
}

# Stable localization keys remain available to other locales and contract tests.
# Administrative/destructive operations belong in advanced help, not this menu.
COMMAND_KEYS = [
    ('start','start'),('today','today'),('next','next'),('random','random'),
    ('search','search'),('translations','edition'),('language','language'),('ui','ui_language'),
    ('subscribe','mode'),('time','time'),('status','status'),('pause','pause'),
    ('resume','resume'),('license','license'),('settings','settings'),('help','help'),
]
COMMAND_DESCRIPTIONS = {
    'ru': ['Открыть главное меню','Прочитать стих дня','Прочитать следующую главу','Прочитать случайный стих',
        'Найти слова в Библии','Выбрать перевод Библии','Выбрать язык Библии','Выбрать язык интерфейса',
        'Настроить ежедневное чтение','Изменить время и часовой пояс','Посмотреть подписки и прогресс',
        'Приостановить рассылку','Возобновить рассылку',
        'Посмотреть источник и лицензию','Открыть настройки','Помощь и примеры команд'],
    'en': ['Open the main menu','Read the verse of the day','Read the next chapter','Read a random verse',
        'Search words in the Bible','Choose a Bible translation','Choose the Bible language','Choose the interface language',
        'Set up daily reading','Change the time and time zone','View subscriptions and progress',
        'Pause deliveries','Resume deliveries',
        'View the source and license','Open settings','Help and command examples'],
}


def commands_for(locale: str) -> list[BotCommand]:
    language = locale or 'ru'
    descriptions = COMMAND_DESCRIPTIONS.get(language)
    commands = [BotCommand(command=command, description=(descriptions[i] if descriptions else tr(language,key))[:256])
        for i,(command,key) in enumerate(COMMAND_KEYS)]
    commands.extend([
        BotCommand(command='donate', description='Добровольно поддержать бота ⭐' if language == 'ru' else 'Support the bot with Stars ⭐'),
        BotCommand(command='paysupport', description='Вопрос по платежу или возврат' if language == 'ru' else 'Payment issue or refund request'),
    ])
    return commands


async def apply_branding(bot: Any, connection: Any, *, avatar_path: Path | None = None) -> bool:
    """Apply changed fields only; record fingerprints only after Telegram accepts.

    A missing avatar or recoverable API failure never stops message processing.
    The background supervisor retries it. Profile-description pictures are a
    separate BotFather feature, not a field configured by these API methods.
    """
    complete = True

    async def update(component: str, value: Any, method: Any, **kwargs: Any) -> None:
        nonlocal complete
        fingerprint = hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        key = f'branding:{bot.id}:{component}'
        if await connection.fetchval('SELECT value FROM app_settings WHERE key=$1',key) == fingerprint:
            return
        try:
            async with asyncio.timeout(20):
                accepted = await method(**kwargs,request_timeout=15)
            if accepted is not True:
                raise ValueError('Telegram did not confirm the profile update')
        except TelegramRetryAfter:
            raise  # The outer loop respects Telegram's requested cooldown.
        except (TelegramAPIError, OSError, TimeoutError, ValueError) as exc:
            LOGGER.warning('Bot branding update deferred: %s (%s)',component,type(exc).__name__)
            complete = False
            return
        await connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',key,fingerprint)

    for locale in ('','ru','en'):
        profile = PROFILE[locale or 'ru']
        for field,method in [('name',bot.set_my_name),('description',bot.set_my_description),
                             ('short_description',bot.set_my_short_description)]:
            await update(f'{field}:{locale}',profile[field],method,language_code=locale,**{field:profile[field]})
    for locale in ['', *sorted(available_ui())]:
        commands = commands_for(locale)
        await update(f'commands:{locale}',[c.model_dump(exclude_none=True) for c in commands],
            bot.set_my_commands,commands=commands,language_code=locale)
    await update('menu','commands',bot.set_chat_menu_button,menu_button=MenuButtonCommands())
    path = avatar_path or AVATAR_PATH
    try:
        avatar_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        LOGGER.warning('Bot avatar deferred: %s (%s)',path.name,type(exc).__name__)
        return False
    await update('avatar',avatar_hash,bot.set_my_profile_photo,
        photo=InputProfilePhotoStatic(photo=FSInputFile(path)))
    return complete


async def branding_loop(bot: Any, pool: Any) -> None:
    """Retry optional branding without blocking or terminating the poller."""
    while True:
        delay = 300
        try:
            async with pool.acquire() as connection:
                complete = await apply_branding(bot,connection)
            if complete:
                delay = 3600
        except TelegramRetryAfter as exc:
            delay = max(delay,exc.retry_after)
            LOGGER.warning('Bot branding rate limited; retry in %s seconds',delay)
        except Exception as exc:
            # Do not log API exception bodies: they may include request secrets.
            LOGGER.warning('Bot branding deferred (%s)',type(exc).__name__)
        await asyncio.sleep(delay)
