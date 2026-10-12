"""Рассылка текста (с необязательной картинкой) всем подходящим назначениям: общий механизм для объявлений,
копий постов из источников и уведомлений о новых видео.

Для каждого назначения под блокировкой чата создаётся задание `delivery_log` с уникальным ключом, поэтому
повторный запуск той же рассылки ничего не дублирует. Дальше работает обычный воркер доставки
(ограничитель скорости, повторы, контрольные точки, разбор неопределённых исходов).
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.formatting import plain_text, split_message, split_wire_message, utf16_length
from app.services.locks import chat_lock

LOGGER = logging.getLogger(__name__)
PLATFORMS = ('telegram', 'max', 'vk')
CHAT_TYPES = ('private', 'group', 'supergroup', 'channel')


def normalize_audience(audience: dict | None) -> dict:
    """Привести аудиторию к допустимому виду: {'platforms': [...], 'chat_types': [...], 'chat_ids': [...]} (пустое — все)."""
    audience = audience or {}
    platforms = [p for p in audience.get('platforms') or [] if p in PLATFORMS]
    kinds = [t for t in audience.get('chat_types') or [] if t in {'private', 'group', 'channel'}]
    ids = [int(x) for x in (audience.get('chat_ids') or [])[:5000] if str(x).lstrip('-').isdigit() and abs(int(x)) < 2**62]
    return {k: v for k, v in (('platforms', platforms), ('chat_types', kinds), ('chat_ids', ids)) if v}


async def recipients(connection: Any, audience: dict | None = None, exclude: tuple = ()) -> list[Any]:
    """Активные назначения по аудитории: личные чаты незаблокированных пользователей, группы и каналы.

    Группы Telegram бывают `group` и `supergroup` — для фильтра это один вид «group».
    """
    audience = normalize_audience(audience)
    only = audience.get('chat_ids', [])
    rows = await connection.fetch('''SELECT c.* FROM telegram_chats c
        LEFT JOIN telegram_users u ON c.chat_type='private' AND u.telegram_user_id=c.telegram_chat_id
        WHERE c.is_active AND NOT COALESCE(u.is_blocked,false)
        AND (cardinality($1::text[])=0 OR c.platform=ANY($1::text[]))
        AND (cardinality($2::text[])=0 OR CASE WHEN c.chat_type='supergroup' THEN 'group' ELSE c.chat_type END=ANY($2::text[]))
        AND (cardinality($3::bigint[])=0 OR c.telegram_chat_id=ANY($3::bigint[]))
        AND NOT (c.telegram_chat_id=ANY($4::bigint[]))
        ORDER BY c.telegram_chat_id''', audience.get('platforms', []), audience.get('chat_types', []), only,
        [int(x) for x in exclude])
    return rows


async def count_recipients(connection: Any, audience: dict | None = None) -> int:
    """Сколько назначений получит рассылка (без загрузки самих строк)."""
    audience = normalize_audience(audience)
    return int(await connection.fetchval('''SELECT count(*) FROM telegram_chats c
        LEFT JOIN telegram_users u ON c.chat_type='private' AND u.telegram_user_id=c.telegram_chat_id
        WHERE c.is_active AND NOT COALESCE(u.is_blocked,false)
        AND (cardinality($1::text[])=0 OR c.platform=ANY($1::text[]))
        AND (cardinality($2::text[])=0 OR CASE WHEN c.chat_type='supergroup' THEN 'group' ELSE c.chat_type END=ANY($2::text[]))
        AND (cardinality($3::bigint[])=0 OR c.telegram_chat_id=ANY($3::bigint[]))''',
                                      audience.get('platforms', []), audience.get('chat_types', []), audience.get('chat_ids', [])))


def platform_parts(platform: str, text: str, max_length: int = 3900) -> list:
    """Части текста для платформы: Telegram и ВКонтакте — строки, MAX — части `max_text` (лимит сырого HTML)."""
    if platform == 'max':
        return [dict(kind='max_text', text=part) for part in split_wire_message(text, min(max_length, 3600))]
    if platform == 'vk':
        return split_message(text, 4000)
    return split_message(text, max_length)


def chunks_for(platform: str, text: str, asset_id: int | None, max_length: int = 3900) -> list:
    """Части сообщения с картинкой: подпись помещается в одно сообщение с фото, иначе фото и текст раздельно."""
    if not asset_id:
        return platform_parts(platform, text, max_length)
    limit = 1024 if platform == 'telegram' else 3600
    fits = (utf16_length(plain_text(text)) <= limit) if platform == 'telegram' else len(text) <= limit
    if fits:
        return [dict(kind='asset_photo', asset_id=asset_id, caption=text)]
    return [dict(kind='asset_photo', asset_id=asset_id, caption=''), *platform_parts(platform, text, max_length)]


async def enqueue(connection: Any, *, key: str, text: str, mode: str, audience: dict | None = None,
                  asset_id: int | None = None, progress: dict | None = None, max_length: int = 3900,
                  exclude: tuple = ()) -> dict[str, int]:
    """Поставить сообщение в очередь доставки каждому назначению аудитории. Возвращает {recipients, queued, skipped}.

    `key` уникален для рассылки: заданию каждого чата присваивается ключ `key`, повторная постановка безопасна.
    """
    from app.worker.delivery import insert_payload
    targets = await recipients(connection, audience, exclude)
    queued = skipped = failed = 0
    for target in targets:
        try:
            async with chat_lock(connection, target['telegram_chat_id']), connection.transaction():
                chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', target['telegram_chat_id'])
                if not chat or not chat['is_active']:
                    skipped += 1
                    continue
                parts = chunks_for(chat['platform'], text, asset_id, max_length)
                await insert_payload(connection, chat, {'id': None}, text, key, mode,
                                     dict(progress or {}, kind=mode), frozen_chunks=parts)
                queued += 1
        except Exception as error:
            skipped += 1
            failed += 1
            LOGGER.warning('Fan-out %s skipped one destination (%s)', key, type(error).__name__)
    return {'recipients': len(targets), 'queued': queued, 'skipped': skipped, 'failed': failed}
