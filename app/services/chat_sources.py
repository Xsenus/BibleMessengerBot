"""Источники контента: посты каналов, групп и сообществ, где состоит бот, и их копирование пользователям.

Бот не читает историю: посты приходят событиями в момент публикации (канал Telegram, где бот администратор; группа или канал
MAX; стена или беседа сообщества ВКонтакте). Неизвестный источник регистрируется выключенным: администратор включает его
и выбирает режим: `capture` (только сохранять), `approve` (сообщить администраторам, они командой `/post ID` отправят копию
всем) или `auto` (копировать пользователям сразу). Копия содержит текст (и первую картинку) без исходной разметки.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services import fanout
from app.services.errors import UserError
from app.services.formatting import escape

LOGGER = logging.getLogger(__name__)
MODES = ('capture', 'approve', 'auto')
MAX_IMAGE = 10 * 1024 * 1024
MIME = {b'\x89PNG\r\n\x1a\n': 'image/png', b'\xff\xd8\xff': 'image/jpeg'}


def mime_of(data: bytes) -> str | None:
    """MIME картинки по заголовку файла (PNG, JPEG, WebP); иначе None."""
    for magic, mime in MIME.items():
        if data.startswith(magic):
            return mime
    return 'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None


async def store_asset(connection: Any, data: bytes, origin: str = 'source') -> int | None:
    """Сохранить картинку в общую таблицу `media_assets` (одинаковые файлы не дублируются). Не картинка или слишком большая — None."""
    mime = mime_of(data) if data and len(data) <= MAX_IMAGE else None
    if not mime:
        return None
    digest = hashlib.sha256(data).hexdigest()
    return await connection.fetchval(
        '''INSERT INTO media_assets(mime_type,data,sha256,origin) VALUES($1,$2,$3,$4)
           ON CONFLICT(sha256) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id''', mime, data, digest, origin)


def safe_image_url(url: str) -> bool:
    """Допустим ли адрес картинки для скачивания: HTTPS, не IP-адрес из частных сетей и не localhost (защита от SSRF)."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or parsed.username or parsed.port not in {None, 443}:
        return False
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host                                   # имена без точки (docker-хосты вроде `postgres`) не допускаются
    return address.is_global


DNS_CHECK = True            # в тестах отключается: имена там вымышленные


async def public_host(url: str) -> bool:
    """Адрес допустим И все адреса, в которые превращается его имя, — публичные (иначе внутреннее имя, смотрящее на частную сеть,
    обошло бы проверку). Остаётся теоретическое окно подмены DNS между проверкой и соединением."""
    if not safe_image_url(url):
        return False
    host = urlsplit(url).hostname or ''
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if not DNS_CHECK:
        return True
    import asyncio
    import socket
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def download_image(url: str, client: httpx.AsyncClient | None = None) -> bytes | None:
    """Скачать картинку по HTTPS без перенаправлений с ограничением размера; при любой проблеме — None."""
    if not await public_host(url):
        return None
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
    try:
        async with client.stream('GET', url) as response:
            if response.status_code != 200:
                return None
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > MAX_IMAGE:
                    return None
            return bytes(data)
    except (httpx.HTTPError, OSError):
        return None
    finally:
        if owned:
            await client.aclose()


async def source_chat_id(connection: Any, platform: str, external_id: int) -> int | None:
    """Resolve the registered destination without treating a bridge's external ID as Telegram's ID."""
    if platform == 'telegram':
        return await connection.fetchval(
            "SELECT telegram_chat_id FROM telegram_chats WHERE platform='telegram' AND telegram_chat_id=$1", external_id)
    return await connection.fetchval(
        "SELECT internal_id FROM platform_identities WHERE platform=$1 AND kind='chat' AND external_id=$2",
        platform, external_id)


async def ensure_source(connection: Any, platform: str, external_id: int, title: str = '', chat_id: int | None = None) -> Any:
    """Найти источник или зарегистрировать его выключенным (администратор включает его в панели)."""
    if chat_id is None:
        chat_id = await source_chat_id(connection, platform, external_id)
    row = await connection.fetchrow('SELECT * FROM content_sources WHERE platform=$1 AND external_id=$2', platform, external_id)
    if row and (not title or title[:200] == row['title']) and (chat_id is None or row['chat_id'] == chat_id):
        return row                                         # частый путь: без записи в БД на каждое сообщение
    return await connection.fetchrow(
        '''INSERT INTO content_sources(platform,external_id,title,chat_id) VALUES($1,$2,$3,$4)
           ON CONFLICT(platform,external_id) DO UPDATE SET
             title=CASE WHEN EXCLUDED.title<>'' THEN EXCLUDED.title ELSE content_sources.title END,
             chat_id=COALESCE(EXCLUDED.chat_id,content_sources.chat_id)
           RETURNING *''', platform, external_id, title[:200], chat_id)


def render_post(source: Any, body: str) -> str:
    """Текст копии: необязательная приставка источника и экранированный текст поста."""
    prefix = f'<b>{escape(source["prefix"])}</b>\n\n' if source['prefix'] else ''
    return prefix + escape(body.strip())


async def ingest_post(connection: Any, platform: str, external_id: Any, message_id: str, text: str, meta: dict | None = None,
                      *, posted_at: Any = None, title: str = '', image: bytes | None = None, image_url: str | None = None) -> int | None:
    """Принять пост источника: зарегистрировать источник (выключенным), а для включённого — сохранить пост и, по режиму,
    сообщить администраторам или сразу отправить копию. Возвращает номер нового поста или None."""
    if type(external_id) is not int or not message_id:
        return None
    source = await ensure_source(connection, platform, external_id, title)
    body = (text or '').strip()
    if not source['is_enabled'] or len(body) < source['min_length'] and not image and not image_url:
        return None
    asset = None
    if image is None and image_url:
        image = await download_image(image_url)
    if image:
        asset = await store_asset(connection, image)
    if not body and asset is None:
        return None
    try:
        when = datetime.fromtimestamp(posted_at, UTC) if isinstance(posted_at, int) else datetime.now(UTC)
    except (ValueError, OverflowError, OSError):          # например, время в миллисекундах
        when = datetime.now(UTC)
    post_id = await connection.fetchval(
        '''INSERT INTO source_posts(source_id,external_id,body,asset_id,meta,posted_at) VALUES($1,$2,$3,$4,$5::jsonb,$6)
           ON CONFLICT(source_id,external_id) DO NOTHING RETURNING id''',
        source['id'], str(message_id)[:100], body[:3500], asset, json.dumps(meta or {}), when)
    if post_id is None:
        return None
    if source['mode'] == 'auto':
        await publish(connection, post_id)
    elif source['mode'] == 'approve':
        await notify_admins(connection, source, post_id, body)
    return post_id


async def notify_admins(connection: Any, source: Any, post_id: int, body: str) -> None:
    """Сообщить администраторам о новом посте и подсказать команды `/post ID` и `/post ignore ID`."""
    admins = [r['telegram_user_id'] for r in await connection.fetch(
        'SELECT telegram_user_id FROM telegram_users WHERE (is_owner OR is_admin) AND NOT is_blocked')]
    if not admins:
        return
    preview = escape(body[:300] + ('…' if len(body) > 300 else ''))
    text = (f"📡 <b>Новый пост</b> · {escape(source['title'] or source['platform'])}\n\n{preview}\n\n"
            f"Отправить всем: <code>/post {post_id}</code>\nПропустить: <code>/post ignore {post_id}</code>")
    await fanout.enqueue(connection, key=f'source-notice:{post_id}', text=text, mode='announcement',
                         audience={'chat_ids': admins, 'chat_types': ['private']})


async def publish(connection: Any, post_id: int, audience: dict | None = None) -> dict[str, int]:
    """Отправить копию поста аудитории источника (или заданной): исходный чат исключается, повтор не дублирует."""
    # Commit intent separately from enqueue. A process stopped at either boundary can be
    # resumed by composer; ordinary captured/awaiting-approval posts have no such intent.
    async with connection.transaction():
        post = await connection.fetchrow('''SELECT p.*,s.audience AS source_audience
            FROM source_posts p JOIN content_sources s ON s.id=p.source_id WHERE p.id=$1 FOR UPDATE OF p''', post_id)
        if not post:
            raise UserError('no_result')
        if post['status'] != 'new':
            raise UserError('invalid')
        if post['publication_requested_at'] is None:
            chosen = audience if audience is not None else post['source_audience']
            chosen = json.loads(chosen) if isinstance(chosen, str) else chosen
            await connection.execute('''UPDATE source_posts SET publication_requested_at=now(),publication_audience=$2::jsonb,
                publication_attempts=0,publication_retry_at=now(),publication_error='' WHERE id=$1''',
                post_id, json.dumps(fanout.normalize_audience(chosen)))
        elif post['publication_attempts'] >= 5:
            # A deliberate manual retry restarts a bounded retry cycle, preserving the
            # confirmed audience and the unique keys of already enqueued destinations.
            await connection.execute('''UPDATE source_posts SET publication_attempts=0,publication_retry_at=now(),
                publication_error='' WHERE id=$1''', post_id)
    return await deliver_post(connection, post_id)


async def deliver_post(connection: Any, post_id: int) -> dict[str, int]:
    """Complete a previously authorized publication; concurrent callers serialize on the post."""
    try:
        async with connection.transaction():
            post = await connection.fetchrow('''SELECT p.*,s.chat_id,s.prefix,s.title,s.platform,s.external_id AS source_external_id
                FROM source_posts p JOIN content_sources s ON s.id=p.source_id WHERE p.id=$1 FOR UPDATE OF p''', post_id)
            if not post or post['status'] != 'new' or post['publication_requested_at'] is None:
                raise UserError('invalid')
            audience = post['publication_audience']
            audience = json.loads(audience) if isinstance(audience, str) else audience
            origin = post['chat_id'] or await source_chat_id(connection, post['platform'], post['source_external_id'])
            counts = await fanout.enqueue(connection, key=f'source-post:{post_id}', text=render_post(post, post['body']),
                                          mode='source_post', audience=audience, asset_id=post['asset_id'], progress={'post_id': post_id},
                                          exclude=(origin,) if origin is not None else ())
            await connection.execute('''UPDATE source_posts SET status=$2,published_at=CASE WHEN $2='published' THEN now() END,
                publication_attempts=publication_attempts+1,publication_retry_at=CASE WHEN $3 AND publication_attempts<4
                THEN now()+interval '1 minute' END,publication_error=$4,recipients=$5,queued=$6,skipped=$7 WHERE id=$1''',
                post_id, 'new' if counts['failed'] else 'published', bool(counts['failed']),
                'Destinations awaiting enqueue' if counts['failed'] else '', counts['recipients'], counts['queued'], counts['skipped'])
            return counts
    except UserError:
        raise
    except Exception as error:
        await connection.execute('''UPDATE source_posts SET publication_attempts=publication_attempts+1,
            publication_retry_at=CASE WHEN publication_attempts<4 THEN now()+interval '1 minute' END,
            publication_error=$2 WHERE id=$1 AND status='new' ''', post_id, type(error).__name__)
        raise


async def retry_publications(connection: Any, limit: int = 5) -> int:
    """Retry only publications with committed approval, with a one-minute delay and a five-attempt ceiling."""
    rows = await connection.fetch('''SELECT id FROM source_posts WHERE status='new' AND publication_requested_at IS NOT NULL
        AND publication_attempts<5 AND publication_retry_at<=now() ORDER BY publication_retry_at,id LIMIT $1''', limit)
    done = 0
    for row in rows:
        try:
            counts = await deliver_post(connection, row['id'])
            done += not counts['failed']
        except UserError:
            pass  # Another recovery worker completed this publication.
        except Exception as error:
            LOGGER.warning('Post %s publication retry failed (%s)', row['id'], type(error).__name__)
    return done


async def ignore(connection: Any, post_id: int) -> bool:
    """Пометить пост пропущенным (копия отправляться не будет)."""
    return await connection.execute("UPDATE source_posts SET status='ignored' WHERE id=$1 AND status='new'", post_id) == 'UPDATE 1'


async def command(connection: Any, actor_id: int, args: list[str], ru: bool = True) -> str:
    """Команда `/post ID` (отправить копию поста всем) и `/post ignore ID` (пропустить)."""
    from app.services.accounts import require_role
    await require_role(connection, actor_id, 'editor')
    if len(args) == 2 and args[0] == 'ignore' and args[1].isdigit():
        ok = await ignore(connection, int(args[1]))
        return ('Пост пропущен.' if ru else 'Post ignored.') if ok else ('Не найден.' if ru else 'Not found.')
    if len(args) == 1 and args[0].isdigit():
        counts = await publish(connection, int(args[0]))
        return (f"✅ Копия поста поставлена в очередь: {counts['queued']} из {counts['recipients']}." if ru else
                f"✅ Post copy queued: {counts['queued']} of {counts['recipients']}.")
    raise UserError('invalid')


async def listing(connection: Any, chat: Any) -> tuple[str, Any]:
    """Раздел «Источники» панели: список источников с кнопками «включить/выключить» и «режим»."""
    from aiogram.types import InlineKeyboardMarkup

    from app.bot.common import button
    ru = chat['ui_language'] == 'ru'
    cid = chat['telegram_chat_id']
    rows = await connection.fetch('SELECT * FROM content_sources ORDER BY is_enabled DESC,id DESC LIMIT 12')
    lines = ['📡 <b>' + ('Источники контента' if ru else 'Content sources') + '</b>']
    keyboard = []
    for row in rows:
        lines.append(f"#{row['id']} {'✅' if row['is_enabled'] else '⛔'} {row['platform']} · {escape(row['title'] or str(row['external_id']))} · {row['mode']}")
        keyboard.append([button(('Выкл ' if row['is_enabled'] else 'Вкл ') + f"#{row['id']}", 'adm', cid, f"sx{row['id']}"),
                         button(f"Режим #{row['id']}", 'adm', cid, f"sm{row['id']}")])
    if not rows:
        lines.append('Источников пока нет: добавьте бота администратором в канал или группу.' if ru
                     else 'No sources yet: add the bot as an administrator to a channel or group.')
    keyboard.append([button('◀ Меню' if ru else '◀ Menu', 'adm', cid, 'home')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


async def section(connection: Any, actor_id: int, chat: Any, value: str = '') -> tuple[str, Any]:
    """Раздел «Источники» и действия (`sx<ID>` включить/выключить, `sm<ID>` сменить режим по кругу)."""
    if value[:2] == 'sx' and value[2:].isdigit():
        await connection.execute('UPDATE content_sources SET is_enabled=NOT is_enabled WHERE id=$1', int(value[2:]))
    elif value[:2] == 'sm' and value[2:].isdigit():
        await connection.execute(
            "UPDATE content_sources SET mode=CASE mode WHEN 'capture' THEN 'approve' WHEN 'approve' THEN 'auto' ELSE 'capture' END WHERE id=$1",
            int(value[2:]))
    return await listing(connection, chat)
