"""Меню администратора в самом боте (Telegram, MAX, ВКонтакте): настройки доступны без веб-панели.

Разделы подключаются словарём SECTIONS: каждый раздел — функция `(connection, actor_id, chat, argument) → (текст, клавиатура)`.
Права проверяются при каждом обращении: открыть меню или нажать кнопку может только администратор или владелец,
управлять списком администраторов — только владелец.
"""
from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot.common import button
from app.services import accounts
from app.services.errors import UserError
from app.services.formatting import escape


def _ru(chat: Any) -> bool:
    return chat['ui_language'] == 'ru'


def back_row(chat: Any) -> list:
    """Кнопка «Назад» в меню администратора."""
    return [button('◀ Меню' if _ru(chat) else '◀ Menu', 'adm', chat['telegram_chat_id'], 'home')]


async def home(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Главное меню администратора: разделы настроек."""
    ru, cid = _ru(chat), chat['telegram_chat_id']
    rows = [[button('📣 Объявления' if ru else '📣 Announcements', 'adm', cid, 'ann'),
             button('📡 Источники' if ru else '📡 Sources', 'adm', cid, 'src')],
            [button('🎬 Видео' if ru else '🎬 Videos', 'adm', cid, 'vid'),
             button('📊 Состояние' if ru else '📊 Status', 'adm', cid, 'stats')],
            [button('👥 Администраторы' if ru else '👥 Administrators', 'adm', cid, 'admins')]]
    text = ('🛠 <b>Панель администратора</b>\nВыберите раздел. Полные настройки — в веб-панели.' if ru
            else '🛠 <b>Administrator panel</b>\nChoose a section. Full settings are in the web panel.')
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admins(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Список администраторов; владелец видит кнопки «Пригласить» и «Снять»."""
    ru, cid = _ru(chat), chat['telegram_chat_id']
    owner = await accounts.is_owner(connection, actor_id)
    lines = ['👥 <b>' + ('Администраторы' if ru else 'Administrators') + '</b>']
    rows = []
    for item in await accounts.list_admins(connection):
        name = escape(item['first_name'] or item['username'] or str(item['telegram_user_id']))
        lines.append(f"{'👑' if item['is_owner'] else '•'} {name} · {item['platform']} · <code>{item['telegram_user_id']}</code>")
        if owner and not item['is_owner']:
            rows.append([button(('Снять: ' if ru else 'Remove: ') + (item['first_name'] or str(item['telegram_user_id']))[:20],
                                'adm', cid, f"rm{item['telegram_user_id']}")])
    if owner:
        rows.append([button('➕ Пригласить' if ru else '➕ Invite', 'adm', cid, 'invite')])
    rows.append(back_row(chat))
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def invite(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Создать одноразовый код приглашения (владелец). Новый администратор отправляет боту `/claim КОД` на любой платформе."""
    code = await accounts.create_admin_invite(connection, actor_id)
    ru = _ru(chat)
    text = (f'Код приглашения (действует 24 часа, одноразовый):\n<code>{code}</code>\n\n'
            'Новый администратор отправляет боту в личном чате (Telegram, MAX или ВКонтакте):\n'
            f'<code>/claim {code}</code>' if ru else
            f'Invitation code (valid 24 hours, single use):\n<code>{code}</code>\n\n'
            f'The new administrator sends the bot in a private chat (Telegram, MAX or VK):\n<code>/claim {code}</code>')
    return text, InlineKeyboardMarkup(inline_keyboard=[back_row(chat)])


async def remove(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Снять права администратора по внутреннему идентификатору (только владелец)."""
    done = await accounts.remove_admin(connection, actor_id, int(argument))
    text = ('Права администратора сняты.' if _ru(chat) else 'Administrator rights removed.') if done else (
        'Не найден.' if _ru(chat) else 'Not found.')
    return text, InlineKeyboardMarkup(inline_keyboard=[back_row(chat)])


async def stats(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Краткая сводка: активные назначения по платформам, доставки за сутки, очередь и объявления."""
    ru = _ru(chat)
    platforms = await connection.fetch(
        'SELECT platform,count(*) AS n FROM telegram_chats WHERE is_active GROUP BY platform ORDER BY platform')
    sent = await connection.fetchval("SELECT count(*) FROM delivery_log WHERE status='sent' AND sent_at>now()-interval '1 day'")
    queue = await connection.fetchval("SELECT count(*) FROM delivery_log WHERE status IN ('pending','retry','sending')")
    failed = await connection.fetchval("SELECT count(*) FROM delivery_log WHERE status IN ('failed','uncertain')")
    lines = ['📊 <b>' + ('Состояние' if ru else 'Status') + '</b>',
             ('Активных назначений: ' if ru else 'Active destinations: ') + ', '.join(f"{r['platform']} {r['n']}" for r in platforms),
             f"{'Отправлено за сутки' if ru else 'Sent in 24h'}: {sent}",
             f"{'В очереди' if ru else 'Queued'}: {queue}",
             f"{'Требуют внимания' if ru else 'Need attention'}: {failed}"]
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=[back_row(chat)])


async def announcements_section(connection: Any, actor_id: int, chat: Any, argument: str = '') -> tuple[str, InlineKeyboardMarkup]:
    """Раздел «Объявления» (см. services.announcements.section)."""
    from app.services import announcements
    return await announcements.section(connection, actor_id, chat, argument)


SECTIONS = {'home': home, 'admins': admins, 'invite': invite, 'stats': stats, 'ann': announcements_section}


async def callback(connection: Any, actor_id: int, chat: Any, value: str) -> tuple[str, InlineKeyboardMarkup]:
    """Обработать нажатие кнопки меню администратора (`adm:раздел[:аргумент]`); права проверяются заново."""
    if not await accounts.is_admin(connection, actor_id):
        raise UserError('forbidden')
    if value.startswith('rm'):
        return await remove(connection, actor_id, chat, value[2:])
    if value[:2] in {'ap', 'ar', 'an', 'ad'} and value[2:].isdigit():
        return await announcements_section(connection, actor_id, chat, value)
    section = SECTIONS.get(value)
    if section is None:
        raise UserError('invalid')
    return await section(connection, actor_id, chat, '')


async def command(connection: Any, actor_id: int, chat: Any, args: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Команда `/admin [invite|list|remove ID]`: только в личном чате и только администратору."""
    if not await accounts.is_admin(connection, actor_id):
        raise UserError('forbidden')
    if chat['chat_type'] != 'private':
        raise UserError('private_only')
    if not args:
        return await home(connection, actor_id, chat)
    if args[0] == 'invite':
        return await invite(connection, actor_id, chat)
    if args[0] == 'list':
        return await admins(connection, actor_id, chat)
    if args[0] == 'remove' and len(args) == 2 and args[1].lstrip('-').isdigit():
        return await remove(connection, actor_id, chat, args[1])
    raise UserError('invalid')
