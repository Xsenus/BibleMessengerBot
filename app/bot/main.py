"""aiogram long-polling process."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import router
from app.config import Settings
from app.db import close_pool, create_pool, wait_for_database
from app.logging import configure_logging

LOGGER = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    await wait_for_database(settings)
    pool = await create_pool(settings)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["db_pool"] = pool
    dispatcher["settings"] = settings

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="today", description="Стих дня"),
            BotCommand(command="next", description="Следующая глава"),
            BotCommand(command="random", description="Случайный стих"),
            BotCommand(command="topic", description="Стих по теме"),
            BotCommand(command="translations", description="Языки и переводы"),
            BotCommand(command="subscribe", description="Настроить рассылку"),
            BotCommand(command="status", description="Состояние подписки"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    LOGGER.info("Starting Telegram long polling")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=True,
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
