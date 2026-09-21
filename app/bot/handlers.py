"""Telegram commands for reading and scheduled publication."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import asyncpg
from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from app.catalog.profiles import normalize_language_code
from app.config import Settings
from app.services.accounts import claim_owner, upsert_chat, upsert_user
from app.services.bible import (
    chat_translation,
    list_translations,
    next_for_user,
    random_verse,
    render_verse,
    search_verses,
    topic_verse,
    user_translation,
    verse_of_day,
)
from app.services.formatting import escape, split_message
from app.services.scheduling import parse_hhmm, validate_timezone
from app.services.subscriptions import (
    create_or_update_subscription,
    delete_subscriptions,
    list_subscriptions,
    set_enabled,
)

router = Router(name="commands")

MODE_ALIASES = {
    "sequential": "sequential",
    "order": "sequential",
    "порядок": "sequential",
    "verse": "verse_of_day",
    "verse_of_day": "verse_of_day",
    "стих": "verse_of_day",
    "topic": "topic_of_day",
    "topic_of_day": "topic_of_day",
    "тема": "topic_of_day",
    "plan": "reading_plan",
    "reading_plan": "reading_plan",
    "план": "reading_plan",
}


async def _register_context(
    message: Message,
    pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    if message.from_user:
        async with pool.acquire() as connection:
            await upsert_user(
                connection,
                user_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
                language_code=message.from_user.language_code,
                default_timezone=settings.default_timezone,
            )
            await upsert_chat(
                connection,
                chat_id=message.chat.id,
                chat_type=message.chat.type.value,
                title=message.chat.title,
                username=message.chat.username,
                registered_by=message.from_user.id,
                default_timezone=settings.default_timezone,
            )


async def _can_manage(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def _require_manage(message: Message, bot: Bot) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True
    if not message.from_user:
        return False
    try:
        allowed = await _can_manage(bot, message.chat.id, message.from_user.id)
    except Exception:  # noqa: BLE001 - Telegram may temporarily reject membership lookup
        allowed = False
    if not allowed:
        await message.answer("Настройки группы или канала может менять только администратор.")
    return allowed


async def _send_chunks(message: Message, text: str, limit: int) -> None:
    for chunk in split_message(text, limit):
        await message.answer(chunk)


async def _selected_translation(
    message: Message,
    connection: asyncpg.Connection,
) -> asyncpg.Record | None:
    language = message.from_user.language_code if message.from_user else None
    if message.chat.type == ChatType.PRIVATE and message.from_user:
        return await user_translation(connection, message.from_user.id, language)
    return await chat_translation(
        connection,
        message.chat.id,
        fallback_language=normalize_language_code(language),
    )


@router.message(CommandStart())
async def start(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    text = (
        "<b>Bible Messenger</b>\n\n"
        "Бот читает Библию по порядку, присылает стих или тему дня и может "
        "публиковать сообщения в группах и каналах по расписанию.\n\n"
        "Основные команды:\n"
        "/today — стих дня\n"
        "/next — следующая глава\n"
        "/random — случайный стих\n"
        "/topic — тематический стих\n"
        "/translations — доступные переводы\n"
        "/subscribe — включить ежедневную отправку\n"
        "/status — состояние подписки\n"
        "/help — полная справка"
    )
    await message.answer(text)


@router.message(Command("help"))
async def help_command(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    text = (
        "<b>Команды чтения</b>\n"
        "/today — стих дня\n"
        "/next — следующая глава с сохранением прогресса\n"
        "/random — случайный стих\n"
        "/topic [код] — стих по теме\n"
        "/search текст — поиск по текущему переводу\n\n"
        "<b>Языки и переводы</b>\n"
        "/translations [код языка] — список редакций\n"
        "/translation ID — выбрать редакцию\n\n"
        "<b>Рассылка</b>\n"
        "/subscribe [sequential|verse|topic|plan] [HH:MM] [Timezone]\n"
        "/pause — пауза\n"
        "/resume — возобновить\n"
        "/unsubscribe [режим] — удалить подписку\n"
        "/status — текущие настройки\n"
        "/channel @channel verse 09:00 Timezone — подключить канал\n/register @channel — только зарегистрировать канал\n\n"
        "Пример: <code>/subscribe verse 09:00 Europe/Amsterdam</code>"
    )
    await message.answer(text)


@router.message(Command("translations"))
async def translations_command(
    message: Message,
    command: CommandObject,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    requested_raw = (command.args or "").strip().lower()
    requested_language = normalize_language_code(requested_raw) if requested_raw else None
    async with db_pool.acquire() as connection:
        editions = await list_translations(connection)
    if requested_language:
        editions = [item for item in editions if item.language_code == requested_language]
    if not editions:
        await message.answer("Переводы ещё импортируются или для указанного языка их нет.")
        return
    lines = ["<b>Доступные переводы</b>"]
    for item in editions[:100]:
        lines.append(
            f"<code>{escape(item.source_id)}</code> — {escape(item.language_code)} — "
            f"{escape(item.short_title)} [{escape(item.coverage)}]"
        )
    if len(editions) > 100:
        lines.append(f"…ещё {len(editions) - 100}. Укажите код языка после команды.")
    await _send_chunks(message, "\n".join(lines), settings.max_message_length)


@router.message(Command("translation"))
async def translation_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    identifier = (command.args or "").strip()
    if not identifier:
        await message.answer("Укажите ID перевода. Список: /translations")
        return
    if not await _require_manage(message, bot):
        return
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id, title FROM translations WHERE lower(source_translation_id)=lower($1) AND is_active=true",
            identifier,
        )
        if not row:
            await message.answer("Такой перевод не найден. Проверьте /translations")
            return
        if message.chat.type == ChatType.PRIVATE and message.from_user:
            await connection.execute(
                "UPDATE telegram_users SET default_translation_id=$2, updated_at=now() WHERE telegram_user_id=$1",
                message.from_user.id,
                row["id"],
            )
        await connection.execute(
            "UPDATE telegram_chats SET default_translation_id=$2, updated_at=now() WHERE telegram_chat_id=$1",
            message.chat.id,
            row["id"],
        )
    await message.answer(f"Выбран перевод: <b>{escape(row['title'])}</b>")


@router.message(Command("today"))
async def today_command(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    async with db_pool.acquire() as connection:
        translation = await _selected_translation(message, connection)
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        timezone_name = await connection.fetchval(
            "SELECT timezone FROM telegram_chats WHERE telegram_chat_id=$1", message.chat.id
        ) or settings.default_timezone
        local_date = datetime.now(ZoneInfo(timezone_name)).date()
        row = await verse_of_day(connection, translation, str(message.chat.id), local_date)
        if not row:
            await message.answer("В выбранном переводе не найден текст стихов.")
            return
        text = await render_verse(connection, row, translation)
    await message.answer(text)


@router.message(Command("random"))
async def random_command(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    async with db_pool.acquire() as connection:
        translation = await _selected_translation(message, connection)
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        row = await random_verse(connection, translation)
        if not row:
            await message.answer("В выбранном переводе не найден текст стихов.")
            return
        text = await render_verse(connection, row, translation)
    await message.answer(text)


@router.message(Command("topic"))
async def topic_command(
    message: Message,
    command: CommandObject,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    topic_code = (command.args or "").strip().lower() or None
    async with db_pool.acquire() as connection:
        translation = await _selected_translation(message, connection)
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        timezone_name = await connection.fetchval(
            "SELECT timezone FROM telegram_chats WHERE telegram_chat_id=$1", message.chat.id
        ) or settings.default_timezone
        local_date = datetime.now(ZoneInfo(timezone_name)).date()
        result = await topic_verse(
            connection, translation, topic_code, str(message.chat.id), local_date
        )
        if not result:
            topics = await connection.fetch("SELECT code, title_ru FROM topics ORDER BY code")
            available = ", ".join(f"{row['code']} ({row['title_ru']})" for row in topics)
            await message.answer(f"Тема не найдена или нет стиха в этом переводе.\n{escape(available)}")
            return
        title, row = result
        text = f"<b>Тема дня: {escape(title)}</b>\n\n" + await render_verse(
            connection, row, translation
        )
    await message.answer(text)


@router.message(Command("next"))
async def next_command(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        await message.answer("Личный прогресс чтения доступен в личном чате с ботом.")
        return
    async with db_pool.acquire() as connection:
        translation = await user_translation(
            connection,
            message.from_user.id,
            message.from_user.language_code,
        )
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        result = await next_for_user(connection, message.from_user.id, translation)
    if not result:
        await message.answer("Чтение завершено или в переводе нет следующей главы.")
        return
    await _send_chunks(message, result[2], settings.max_message_length)


@router.message(Command("search"))
async def search_command(
    message: Message,
    command: CommandObject,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    query = (command.args or "").strip()
    if len(query) < 2:
        await message.answer("Пример: <code>/search любовь</code>")
        return
    async with db_pool.acquire() as connection:
        translation = await _selected_translation(message, connection)
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        rows = await search_verses(connection, translation["id"], query)
        if not rows:
            await message.answer("Совпадений не найдено.")
            return
        parts = [await render_verse(connection, row, translation) for row in rows]
    await _send_chunks(message, "\n\n".join(parts), settings.max_message_length)


@router.message(Command("subscribe"))
async def subscribe_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if not message.from_user or not await _require_manage(message, bot):
        return
    args = (command.args or "").split()
    mode = MODE_ALIASES.get(args[0].lower(), "verse_of_day") if args else "verse_of_day"
    time_value = args[1] if len(args) > 1 else settings.default_send_time
    timezone_name = args[2] if len(args) > 2 else settings.default_timezone
    plan_code = args[3] if len(args) > 3 and mode == "reading_plan" else None
    try:
        send_time = parse_hhmm(time_value)
        validate_timezone(timezone_name)
    except ValueError as exc:
        await message.answer(escape(exc))
        return

    async with db_pool.acquire() as connection:
        translation = await _selected_translation(message, connection)
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        row = await create_or_update_subscription(
            connection,
            chat_id=message.chat.id,
            created_by=message.from_user.id,
            translation_id=translation["id"],
            mode=mode,
            send_time=send_time,
            timezone_name=timezone_name,
            plan_code=plan_code,
        )
    await message.answer(
        "Рассылка включена.\n"
        f"Режим: <code>{escape(row['mode'])}</code>\n"
        f"Время: <b>{escape(str(row['send_time'])[:5])}</b>\n"
        f"Часовой пояс: <b>{escape(row['timezone'])}</b>"
    )


@router.message(Command("pause"))
async def pause_command(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if not await _require_manage(message, bot):
        return
    async with db_pool.acquire() as connection:
        count = await set_enabled(connection, message.chat.id, False)
    await message.answer(f"Приостановлено подписок: {count}")


@router.message(Command("resume"))
async def resume_command(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if not await _require_manage(message, bot):
        return
    async with db_pool.acquire() as connection:
        rows = await list_subscriptions(connection, message.chat.id)
        count = 0
        for row in rows:
            await create_or_update_subscription(
                connection,
                chat_id=message.chat.id,
                created_by=message.from_user.id if message.from_user else row["created_by"],
                translation_id=row["translation_id"],
                mode=row["mode"],
                send_time=row["send_time"],
                timezone_name=row["timezone"],
                plan_code=row["plan_code"],
            )
            count += 1
    await message.answer(f"Возобновлено подписок: {count}")


@router.message(Command("unsubscribe"))
async def unsubscribe_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if not await _require_manage(message, bot):
        return
    requested = (command.args or "").strip().lower()
    mode = MODE_ALIASES.get(requested) if requested else None
    async with db_pool.acquire() as connection:
        count = await delete_subscriptions(connection, message.chat.id, mode)
    await message.answer(f"Удалено подписок: {count}")


@router.message(Command("status"))
async def status_command(message: Message, db_pool: asyncpg.Pool, settings: Settings) -> None:
    await _register_context(message, db_pool, settings)
    async with db_pool.acquire() as connection:
        rows = await list_subscriptions(connection, message.chat.id)
    if not rows:
        await message.answer("Активных или сохранённых подписок нет.")
        return
    lines = ["<b>Подписки</b>"]
    for row in rows:
        lines.append(
            f"{escape(row['mode'])}: {'включена' if row['is_enabled'] else 'пауза'}, "
            f"{str(row['send_time'])[:5]} {escape(row['timezone'])}, "
            f"{escape(row['source_translation_id'])}, следующий запуск: "
            f"{escape(row['next_run_at'] or '—')}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("claim"))
async def claim_command(
    message: Message,
    command: CommandObject,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        await message.answer("Команда владельца принимается только в личном чате.")
        return
    supplied = (command.args or "").strip()
    async with db_pool.acquire() as connection:
        claimed = await claim_owner(
            connection,
            telegram_user_id=message.from_user.id,
            supplied_code=supplied,
            expected_code=settings.owner_claim_code,
        )
    await message.answer("Права владельца подтверждены." if claimed else "Код неверен или уже использован.")


@router.message(Command("register"))
async def register_channel_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    await _register_context(message, db_pool, settings)
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        await message.answer("Канал регистрируется из личного чата с ботом.")
        return
    target: str | int = (command.args or "").strip()
    if not target:
        await message.answer("Пример: <code>/register @my_channel</code>")
        return
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    try:
        chat = await bot.get_chat(target)
        if chat.type != ChatType.CHANNEL:
            await message.answer("Указанный объект не является каналом.")
            return
        user_member = await bot.get_chat_member(chat.id, message.from_user.id)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if user_member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer("Вы должны быть администратором этого канала.")
            return
        if bot_member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer("Сначала назначьте бота администратором канала с правом публикации.")
            return
    except Exception as exc:  # noqa: BLE001 - present Telegram validation error to operator
        await message.answer(f"Не удалось проверить канал: {escape(exc)}")
        return

    async with db_pool.acquire() as connection:
        await upsert_chat(
            connection,
            chat_id=chat.id,
            chat_type=chat.type.value,
            title=chat.title,
            username=chat.username,
            registered_by=message.from_user.id,
            default_timezone=settings.default_timezone,
        )
    await message.answer(
        f"Канал <b>{escape(chat.title or chat.id)}</b> зарегистрирован.\n"
        "Чтобы настроить расписание, временно добавьте бота в обсуждение или "
        "используйте административный API/панель."
    )

@router.message(Command("channel"))
async def channel_subscription_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db_pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    """Register a channel and create its schedule from a private conversation."""
    await _register_context(message, db_pool, settings)
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        await message.answer("Команда /channel выполняется в личном чате с ботом.")
        return
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Пример: <code>/channel @my_channel verse 09:00 Europe/Amsterdam</code>"
        )
        return
    target: str | int = args[0]
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    mode = MODE_ALIASES.get(args[1].lower(), "verse_of_day") if len(args) > 1 else "verse_of_day"
    time_value = args[2] if len(args) > 2 else settings.default_send_time
    timezone_name = args[3] if len(args) > 3 else settings.default_timezone
    try:
        send_time = parse_hhmm(time_value)
        validate_timezone(timezone_name)
        chat = await bot.get_chat(target)
        if chat.type != ChatType.CHANNEL:
            await message.answer("Указанный объект не является каналом.")
            return
        user_member = await bot.get_chat_member(chat.id, message.from_user.id)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if user_member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer("Вы должны быть администратором канала.")
            return
        if bot_member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer("Назначьте бота администратором канала с правом публикации.")
            return
    except ValueError as exc:
        await message.answer(escape(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Не удалось проверить канал: {escape(exc)}")
        return

    async with db_pool.acquire() as connection:
        await upsert_chat(
            connection,
            chat_id=chat.id,
            chat_type=chat.type.value,
            title=chat.title,
            username=chat.username,
            registered_by=message.from_user.id,
            default_timezone=timezone_name,
        )
        translation = await user_translation(
            connection,
            message.from_user.id,
            message.from_user.language_code,
        )
        if not translation:
            await message.answer("В базе пока нет импортированного перевода.")
            return
        await connection.execute(
            "UPDATE telegram_chats SET default_translation_id=$2 WHERE telegram_chat_id=$1",
            chat.id,
            translation["id"],
        )
        subscription = await create_or_update_subscription(
            connection,
            chat_id=chat.id,
            created_by=message.from_user.id,
            translation_id=translation["id"],
            mode=mode,
            send_time=send_time,
            timezone_name=timezone_name,
        )
    await message.answer(
        f"Канал <b>{escape(chat.title or chat.id)}</b> подключён.\n"
        f"Режим: <code>{escape(mode)}</code>\n"
        f"Время: <b>{str(subscription['send_time'])[:5]}</b> {escape(timezone_name)}"
    )
