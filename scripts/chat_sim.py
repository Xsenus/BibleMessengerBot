"""Имитатор чата: выполняет команды бота против настоящей базы без Telegram и печатает ответы.

    python scripts/chat_sim.py                       # интерактивно (личный чат с id 1000001)
    python scripts/chat_sim.py --user 42 "/read Ин 3:16" "/bookmark Ин 3:16 важно" /bookmarks
    echo "/streak" | python scripts/chat_sim.py --user 42

Нужны DATABASE_URL и импортированные издания. Сообщения в сети не отправляются; ответы воркера (например, /next, где текст
готовит доставка) показываются как «(поставлено в очередь доставки)». Это проверка логики команд, а не замена проверке в настоящем мессенджере.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot import handlers  # noqa: E402
from app.bot.commands import parse_command  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.services import accounts  # noqa: E402
from app.services.errors import UserError  # noqa: E402
from app.services.i18n import tr  # noqa: E402

TAG = re.compile(r'<[^>]+>')


def show_markup(markup) -> list[str]:
    rows = getattr(markup, 'inline_keyboard', None) or []
    return ['  [' + ' | '.join(b.text for b in row) + ']' for row in rows]


async def send(connection, settings, user_id: int, text: str, counter: list[int]) -> None:
    counter[0] += 1
    chat = SimpleNamespace(id=user_id, type='private', title=None, username=None)
    message = SimpleNamespace(from_user=SimpleNamespace(id=user_id, username='sim', first_name='Sim', last_name=None, language_code='ru', is_bot=False),
                              chat=chat, text=text, message_id=counter[0], message_thread_id=None)
    bot = SimpleNamespace(get_chat=AsyncMock(), get_me=AsyncMock(), get_chat_member=AsyncMock())
    await accounts.upsert_user(connection, user_id=user_id, username='sim', first_name='Sim', last_name=None,
                               language_code='ru', default_timezone=settings.default_timezone)
    await accounts.upsert_chat(connection, chat_id=user_id, chat_type='private', title=None, username=None,
                               registered_by=user_id, default_timezone=settings.default_timezone)
    print(f'> {text}')
    try:
        reply, markup = await handlers.run_command(connection, bot, settings, message, parse_command(text))
    except UserError as error:
        print('  ⚠ ' + (tr('ru', error.key) if error.key else str(error)))
        return
    print('  ' + (TAG.sub('', reply).replace('\n', '\n  ') if reply else '(поставлено в очередь доставки или ответа нет)'))
    print('\n'.join(show_markup(markup)))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', type=int, default=1000001)
    parser.add_argument('commands', nargs='*')
    args = parser.parse_args()
    settings = Settings.from_env(require_bot_token=False)
    pool = await create_pool(settings)
    counter = [0]
    async with pool.acquire() as connection:
        lines = args.commands or ([line.strip() for line in sys.stdin] if not sys.stdin.isatty() else [])
        if lines:
            for line in lines:
                if line:
                    await send(connection, settings, args.user, line, counter)
            return
        print('Имитатор чата. Введите команду (например /start, /read Ин 3:16); пустая строка — выход.')
        while True:
            try:
                line = input('> ').strip()
            except EOFError:
                break
            if not line:
                break
            await send(connection, settings, args.user, line, counter)


if __name__ == '__main__':
    asyncio.run(main())
