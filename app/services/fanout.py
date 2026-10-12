"""Рассылка текста (с необязательной картинкой) всем подходящим назначениям: общий механизм для объявлений,
копий постов из источников и уведомлений о новых видео.

Для каждого назначения под блокировкой чата создаётся задание `delivery_log` с уникальным ключом, поэтому
повторный запуск той же рассылки ничего не дублирует. Дальше работает обычный воркер доставки
(ограничитель скорости, повторы, контрольные точки, разбор неопределённых исходов).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services.formatting import plain_text, split_message, utf16_length
from app.services.locks import chat_lock
from app.services.platforms import ALL

LOGGER = logging.getLogger(__name__)
PLATFORMS = ALL
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
    """Части текста для платформы: Telegram — строки; мостовые платформы режут текст по правилам своего описания (`PlatformSpec.split`)."""
    from app.platforms import REGISTRY
    spec = REGISTRY.get(platform)
    if spec is not None and spec.split is not None:
        return spec.split(text, max_length)
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


BATCH = 500


async def _enqueue_one(connection: Any, target: Any, text: str, key: str, mode: str, progress: dict, asset_id: int | None,
                       max_length: int) -> bool:
    """Запасной путь для одного чата (с блокировкой и повторной проверкой): используется, когда пакетная вставка не удалась."""
    from app.worker.delivery import insert_payload
    async with chat_lock(connection, target['telegram_chat_id']), connection.transaction():
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1', target['telegram_chat_id'])
        if not chat or not chat['is_active']:
            return False
        await insert_payload(connection, chat, {'id': None}, text, key, mode, dict(progress, kind=mode),
                             frozen_chunks=chunks_for(chat['platform'], text, asset_id, max_length))
        return True


async def enqueue(connection: Any, *, key: str, text: str, mode: str, audience: dict | None = None,
                  asset_id: int | None = None, progress: dict | None = None, max_length: int = 3900,
                  exclude: tuple = ()) -> dict[str, int]:
    """Поставить сообщение в очередь доставки каждому назначению аудитории. Возвращает {recipients, queued, skipped, failed}.

    `key` уникален для рассылки: заданию каждого чата присваивается ключ `key`, повторная постановка безопасна
    (`UNIQUE(чат, ключ)`). Части сообщения считаются один раз на платформу, а запись идёт пакетами по 500 чатов одной
    вставкой — это в десятки раз быстрее, чем по одному чату с блокировкой. Если пакет не прошёл, его чаты ставятся по одному.
    """
    targets = await recipients(connection, audience, exclude)
    progress_json = json.dumps(dict(progress or {}, kind=mode))
    parts: dict[str, str] = {}                                    # платформа → JSON частей (одинаковый для всех её чатов)
    queued = skipped = failed = 0
    for start in range(0, len(targets), BATCH):
        batch = targets[start:start + BATCH]
        for target in batch:
            if target['platform'] not in parts:
                parts[target['platform']] = json.dumps(chunks_for(target['platform'], text, asset_id, max_length), ensure_ascii=False)
        try:
            async with connection.transaction():
                await connection.execute(
                    '''INSERT INTO delivery_log(telegram_chat_id,mode,payload_key,scheduled_for,status,payload_preview,chunks,progress,
                           chat_revision,ui_language,message_thread_id)
                       SELECT t.chat_id,$2,$3,now(),'pending',$4,t.chunks::jsonb,$5::jsonb,t.revision,t.ui_language,t.thread
                       FROM unnest($1::bigint[],$6::text[],$7::int[],$8::text[],$9::bigint[]) AS t(chat_id,chunks,revision,ui_language,thread)
                       ON CONFLICT(telegram_chat_id,payload_key) DO NOTHING''',
                    [t['telegram_chat_id'] for t in batch], mode, key, text[:1000], progress_json,
                    [parts[t['platform']] for t in batch], [t['revision'] for t in batch], [t['ui_language'] for t in batch],
                    [t['message_thread_id'] for t in batch])
            queued += len(batch)
        except Exception as error:
            LOGGER.warning('Fan-out %s batch failed (%s); falling back to one-by-one', key, type(error).__name__)
            for target in batch:
                try:
                    if await _enqueue_one(connection, target, text, key, mode, progress or {}, asset_id, max_length):
                        queued += 1
                    else:
                        skipped += 1
                except Exception as inner:
                    skipped += 1
                    failed += 1
                    LOGGER.warning('Fan-out %s skipped one destination (%s)', key, type(inner).__name__)
    return {'recipients': len(targets), 'queued': queued, 'skipped': skipped, 'failed': failed}
