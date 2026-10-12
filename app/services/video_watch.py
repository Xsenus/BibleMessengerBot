"""Слежение за видеоканалами YouTube, RuTube и ВКонтакте: опрос по расписанию, запоминание уже вышедших видео и рассылка ссылок.

Первый опрос канала только запоминает текущие видео (рассылки задним числом нет). Новые видео записываются один раз
(UNIQUE feed_id+external_id); рассылка идемпотентна по ключу `video:<id>`. Сбои сети не останавливают службу:
интервал опроса растёт (экспоненциально, не более 6 часов), причина хранится в `last_error`.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

import httpx

from app.net.transport import routed_client
from app.services import fanout
from app.services.errors import UserError

LOGGER = logging.getLogger(__name__)
PLATFORMS = ('youtube', 'rutube', 'vk')
MAX_BODY = 1_000_000
MAX_NOTIFY_PER_POLL = 3
BACKOFF_CAP_MINUTES = 360
YOUTUBE_URL = 'https://www.youtube.com/feeds/videos.xml?channel_id={}'
RUTUBE_URL = 'https://rutube.ru/api/video/person/{}/?format=json'
NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}


def parse_handle(platform: str, value: str) -> str:
    """Выделить идентификатор канала из ввода администратора (сам ID или ссылка); неверный формат — UserError."""
    value = value.strip()
    if platform == 'youtube':
        found = re.search(r'(UC[\w-]{22})', value)
    elif platform == 'rutube':
        found = re.search(r'(?:channel/|person/)?\b([1-9][0-9]{0,15})\b', value)
    elif platform == 'vk':
        found = re.search(r'(?:club|public|-)([1-9][0-9]{0,15})|\b([1-9][0-9]{0,15})\b', value)
        if found:
            number = found.group(1) or found.group(2)
            return ('-' if found.group(1) else '') + number
    else:
        raise UserError('invalid')
    if not found:
        raise UserError('invalid')
    return found.group(1)


def parse_youtube(body: bytes) -> list[dict]:
    """Разобрать Atom-ленту YouTube. Документы с DOCTYPE/ENTITY отвергаются (защита от XML-бомб)."""
    if b'<!DOCTYPE' in body[:2000].upper() or b'<!ENTITY' in body.upper():
        raise ValueError('unsafe xml')
    root = ET.fromstring(body)
    items = []
    for entry in root.findall('a:entry', NS):
        vid = entry.findtext('yt:videoId', default='', namespaces=NS)
        if not re.fullmatch(r'[\w-]{11}', vid):
            continue
        items.append({'id': vid, 'title': clean(entry.findtext('a:title', default='', namespaces=NS)),
                      'url': f'https://www.youtube.com/watch?v={vid}', 'published': _time(entry.findtext('a:published', namespaces=NS))})
    return items


def parse_rutube(data: Any) -> list[dict]:
    """Разобрать ответ API RuTube: список видео автора."""
    items = []
    for row in (data.get('results') if isinstance(data, dict) else None) or []:
        vid = str(row.get('id', '')) if isinstance(row, dict) else ''
        if re.fullmatch(r'[0-9a-f]{32}', vid):
            items.append({'id': vid, 'title': clean(row.get('title', '')), 'url': f'https://rutube.ru/video/{vid}/',
                          'published': _time(row.get('created_ts') or row.get('publication_ts'))})
    return items


def parse_vk(data: Any) -> list[dict]:
    """Разобрать ответ `video.get`."""
    items = []
    for row in (data.get('items') if isinstance(data, dict) else None) or []:
        if isinstance(row, dict) and type(row.get('id')) is int and type(row.get('owner_id')) is int:
            key = f"{row['owner_id']}_{row['id']}"
            items.append({'id': key, 'title': clean(row.get('title', '')), 'url': f'https://vk.com/video{key}',
                          'published': _time(row.get('date'))})
    return items


def clean(value: Any, limit: int = 300) -> str:
    """Строка для БД: без нулевых символов (PostgreSQL их не принимает) и не длиннее `limit`."""
    return str(value or '').replace('\x00', '')[:limit]


def _time(value: Any) -> datetime | None:
    """Время публикации из ISO-строки или секунд Unix; неразборчивое значение — None."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


async def _get(client: httpx.AsyncClient, url: str) -> bytes:
    """Скачать ответ с ограничением размера и без перенаправлений."""
    async with client.stream('GET', url, headers={'User-Agent': 'BibleMessengerBot/1.0'}) as response:
        response.raise_for_status()
        data = bytearray()
        async for block in response.aiter_bytes():
            data.extend(block)
            if len(data) > MAX_BODY:
                raise ValueError('response too large')
    return bytes(data)


async def fetch(feed: Any, client: httpx.AsyncClient, vk_call: Any = None) -> list[dict]:
    """Получить последние видео канала, новые первыми не гарантируются (порядок определяется временем публикации)."""
    if feed['platform'] == 'youtube':
        items = parse_youtube(await _get(client, YOUTUBE_URL.format(feed['handle'])))
    elif feed['platform'] == 'rutube':
        items = parse_rutube(json.loads(await _get(client, RUTUBE_URL.format(feed['handle']))))
    elif feed['platform'] == 'vk':
        if vk_call is None:
            raise ValueError('VK_SERVICE_TOKEN is not configured')
        items = parse_vk(await vk_call('video.get', owner_id=int(feed['handle']), count=10))
    else:
        raise ValueError('unknown platform')
    return sorted(items, key=lambda i: i['published'] or datetime.min.replace(tzinfo=UTC))


def render(feed: Any, item: dict | Any, ru: bool = True) -> str:
    """Текст уведомления: шаблон канала (`{title}`, `{url}`, `{channel}`) или стандартное сообщение."""
    values = {'title': escape(item['title']), 'url': item['url'], 'channel': escape(feed['title'] or feed['handle'])}
    template = feed['template'] or ('🎬 <b>Новое видео</b>: {title}\n{url}' if ru else '🎬 <b>New video</b>: {title}\n{url}')
    return re.sub(r'\{(title|url|channel)\}', lambda m: values[m.group(1)], template)


async def poll_feed(connection: Any, feed: Any, client: httpx.AsyncClient, vk_call: Any = None) -> int:
    """Опросить один канал и вернуть число поставленных в очередь уведомлений. Любые сбои фиксируются, а не пробрасываются."""
    try:
        items = await fetch(feed, client, vk_call)
    except Exception as error:
        LOGGER.warning('Video feed %s poll failed (%s)', feed['id'], type(error).__name__)
        await _record_failure(connection, feed, type(error).__name__)
        return 0
    queued = 0
    async with connection.transaction():
        fresh = []
        for item in items:
            older = item['published'] is not None and item['published'] < feed['created_at']   # старое видео вернулось в окно ленты
            status = 'skipped' if (not feed['baselined'] or older) else 'new'
            row = await connection.fetchrow('''INSERT INTO video_items(feed_id,external_id,title,url,published_at,status)
                VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(feed_id,external_id) DO NOTHING RETURNING id,status''',
                feed['id'], item['id'], item['title'], item['url'], item['published'], status)
            if row and status == 'new':
                fresh.append((row['id'], item))
        await connection.execute('''UPDATE video_feeds SET baselined=true,error_count=0,last_error='',last_checked_at=now(),
            next_check_at=now()+make_interval(mins=>interval_minutes) WHERE id=$1''', feed['id'])
        for position, (item_id, item) in enumerate(fresh):
            if not feed['notify'] or position < len(fresh) - MAX_NOTIFY_PER_POLL:
                await connection.execute("UPDATE video_items SET status='skipped' WHERE id=$1", item_id)
                continue
            audience = feed['audience']
            audience = json.loads(audience) if isinstance(audience, str) else audience
            await fanout.enqueue(connection, key=f'video:{item_id}', text=render(feed, item), mode='video_notice', audience=audience or {})
            await connection.execute("UPDATE video_items SET status='notified' WHERE id=$1", item_id)
            queued += 1
    return queued


async def claim_due(connection: Any, limit: int = 5) -> list[Any]:
    """Захватить каналы, срок которых наступил: следующая проверка сдвигается на 10 минут, поэтому другая копия службы или
    повторный цикл их не возьмёт; сеть и запись результата выполняются уже вне этой короткой транзакции."""
    return await connection.fetch('''UPDATE video_feeds SET next_check_at=now()+interval '10 minutes' WHERE id IN (
        SELECT id FROM video_feeds WHERE is_enabled AND next_check_at<=now() ORDER BY next_check_at LIMIT $1 FOR UPDATE SKIP LOCKED)
        RETURNING *''', limit)


async def _record_failure(connection: Any, feed: Any, error: str) -> None:
    """Записать сбой опроса и отодвинуть следующую проверку (экспоненциально, не более 6 часов)."""
    count = feed['error_count'] + 1
    delay = min(feed['interval_minutes'] * 2 ** min(count, 8), BACKOFF_CAP_MINUTES)
    await connection.execute('''UPDATE video_feeds SET error_count=$2,last_error=$3,last_checked_at=now(),
        next_check_at=now()+make_interval(mins=>$4) WHERE id=$1''', feed['id'], count, error[:60], delay)


async def run_due(connection: Any, client: httpx.AsyncClient | None = None, vk_call: Any = None, limit: int = 5) -> int:
    """Опросить каналы, срок которых наступил (не более `limit` за вызов). Каждый канал обрабатывается отдельно: сбой одного
    (сеть, база, разбор) записывается в его состояние и не мешает остальным и не вызывает бесконечного повтора."""
    feeds = await claim_due(connection, limit)
    if not feeds:
        return 0
    owned = client is None
    client = client or routed_client(timeout=httpx.Timeout(20, connect=10), follow_redirects=False)
    vk_client = None
    if vk_call is None:
        from app.vkbot.client import VkClient
        from app.vkbot.config import VkSettings
        try:
            settings = VkSettings.from_env()
        except (ValueError, RuntimeError):
            settings = VkSettings()
        if settings.service_token:
            vk_client = VkClient(settings)
            token = settings.service_token

            async def vk_call(method: str, **params: Any) -> Any:
                """Вызов API ВКонтакте сервисным ключом (чтение публичных видео)."""
                return await vk_client.call(method, token=token, **params)
    total = 0
    try:
        for feed in feeds:
            try:
                total += await poll_feed(connection, feed, client, vk_call)
            except Exception as error:
                LOGGER.error('Video feed %s processing failed (%s)', feed['id'], type(error).__name__)
                await _record_failure(connection, feed, type(error).__name__)
    finally:
        if owned:
            await client.aclose()
        if vk_client:
            await vk_client.close()
    return total


async def add(connection: Any, platform: str, value: str, *, notify: bool = False) -> Any:
    """Добавить канал (идемпотентно по платформе и ID)."""
    if platform not in PLATFORMS:
        raise UserError('invalid')
    handle = parse_handle(platform, value)
    return await connection.fetchrow('''INSERT INTO video_feeds(platform,handle,notify) VALUES($1,$2,$3)
        ON CONFLICT(platform,handle) DO UPDATE SET is_enabled=true RETURNING *''', platform, handle, notify)


async def command(connection: Any, actor_id: int, chat: Any, args: list[str]) -> tuple[str, Any]:
    """Команда `/video`: без аргументов — раздел; `add платформа канал [notify]`, `on|off|notify|silent|del ID`."""
    from app.services.accounts import is_admin
    if not await is_admin(connection, actor_id):
        raise UserError('forbidden')
    ru = chat['ui_language'] == 'ru'
    if not args:
        return await section(connection, actor_id, chat, '')
    action = args[0]
    if action == 'add' and len(args) in {3, 4}:
        feed = await add(connection, args[1], args[2], notify=len(args) == 4 and args[3] == 'notify')
        message = f"✅ Канал #{feed['id']} добавлен." if ru else f"✅ Feed #{feed['id']} added."
        return message, None
    if action in {'on', 'off', 'notify', 'silent', 'del'} and len(args) == 2 and args[1].isdigit():
        await _change(connection, action, int(args[1]))
        return ('Готово.' if ru else 'Done.'), None
    raise UserError('invalid')


async def _change(connection: Any, action: str, feed_id: int) -> None:
    """Применить действие к каналу."""
    if action == 'del':
        await connection.execute('DELETE FROM video_feeds WHERE id=$1', feed_id)
        return
    column, value = {'on': ('is_enabled', True), 'off': ('is_enabled', False),
                     'notify': ('notify', True), 'silent': ('notify', False)}[action]
    await connection.execute(f'UPDATE video_feeds SET {column}=$2 WHERE id=$1', feed_id, value)  # noqa: S608 (имя столбца из словаря)


async def section(connection: Any, actor_id: int, chat: Any, value: str = '') -> tuple[str, Any]:
    """Раздел «Видео» панели администратора: список каналов, переключатели «опрос» и «рассылка» (`vt<ID>`, `vn<ID>`)."""
    from aiogram.types import InlineKeyboardMarkup

    from app.bot.common import button
    if value[:2] in {'vt', 'vn'} and value[2:].isdigit():
        column = 'is_enabled' if value[:2] == 'vt' else 'notify'
        await connection.execute(f'UPDATE video_feeds SET {column}=NOT {column} WHERE id=$1', int(value[2:]))  # noqa: S608
    ru = chat['ui_language'] == 'ru'
    cid = chat['telegram_chat_id']
    rows = await connection.fetch('SELECT * FROM video_feeds ORDER BY id DESC LIMIT 10')
    lines = ['🎬 <b>' + ('Видеоканалы' if ru else 'Video channels') + '</b>']
    keyboard = []
    for row in rows:
        lines.append(f"#{row['id']} {'✅' if row['is_enabled'] else '⛔'} {row['platform']} · {escape(row['title'] or row['handle'])}"
                     f" · {'📨' if row['notify'] else '🔕'}" + (f" · ⚠ {row['last_error']}" if row['last_error'] else ''))
        keyboard.append([button(('Опрос ' if ru else 'Poll ') + f"#{row['id']}", 'adm', cid, f"vt{row['id']}"),
                         button(('Рассылка ' if ru else 'Notify ') + f"#{row['id']}", 'adm', cid, f"vn{row['id']}")])
    if not rows:
        lines.append('Каналов нет. Добавить: /video add youtube UC… [notify]' if ru else 'No channels. Add: /video add youtube UC… [notify]')
    keyboard.append([button('◀ Меню' if ru else '◀ Menu', 'adm', cid, 'home')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


def next_poll_hint(now: datetime, minutes: int) -> datetime:
    """Время следующего опроса (для тестов и веб-панели)."""
    return now + timedelta(minutes=minutes)
