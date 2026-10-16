"""Журнал действий администраторов (панель и бот): кто, откуда, что и над чем сделал."""
from __future__ import annotations

import json
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)
SENSITIVE = {'api_key', 'password', 'secret', 'token', 'proxy_url', 'url'}


def clean(details: dict | None) -> dict:
    """Убрать из подробностей поля с секретами (ключи, пароли, адреса прокси) и обрезать длинные значения."""
    out = {}
    for key, value in (details or {}).items():
        if any(word in key.lower() for word in SENSITIVE):
            out[key] = '***'
        else:
            out[key] = value[:200] if isinstance(value, str) else value
    return out


async def record(connection: Any, actor: int | str, source: str, action: str, target: str = '', details: dict | None = None) -> None:
    """Записать действие; сбой журнала никогда не мешает самому действию."""
    try:
        await connection.execute(
            'INSERT INTO admin_audit(actor,source,action,target,details) VALUES($1,$2,$3,$4,$5::jsonb)',
            str(actor), source, action[:100], target[:200], json.dumps(clean(details), default=str))
    except Exception as error:
        LOGGER.warning('Audit write failed (%s)', type(error).__name__)


def _like(text: str) -> str:
    """Экранировать спецсимволы LIKE, чтобы поиск шёл по буквальному тексту."""
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


async def recent(connection: Any, limit: int = 100, offset: int = 0, actor: str = '', action: str = '',
                 source: str = '', q: str = '', days: int = 0) -> list[dict]:
    """Записи журнала, новые сверху. Фильтры: точный исполнитель и источник, начало действия,
    подстрока q в исполнителе, действии или объекте, последние days дней (0 — за всё время)."""
    rows = await connection.fetch(
        """SELECT id,created_at,actor,source,action,target,details FROM admin_audit
           WHERE ($1='' OR actor=$1) AND ($2='' OR action LIKE $2||'%') AND ($5='' OR source=$5)
             AND ($6='' OR actor ILIKE '%'||$6||'%' OR action ILIKE '%'||$6||'%' OR target ILIKE '%'||$6||'%')
             AND ($7=0 OR created_at >= now() - make_interval(days => $7))
           ORDER BY id DESC LIMIT $3 OFFSET $4""",
        actor, action, max(1, min(limit, 500)), max(0, offset), source, _like(q.strip()[:100]), max(0, min(days, 3650)))
    result = []
    for row in rows:
        item = dict(row)
        item['created_at'] = row['created_at'].isoformat()
        if isinstance(item['details'], str):
            item['details'] = json.loads(item['details'])
        result.append(item)
    return result


async def actions(connection: Any) -> list[str]:
    """Различные действия из журнала — для фильтра в панели. Запросы панели («POST /announcements/7/status»)
    сводятся к методу и разделу («POST /announcements»): фильтр action ищет по началу строки и найдёт их все."""
    rows = await connection.fetch(
        r"""SELECT DISTINCT coalesce(substring(action from '^\S+ /[^/]+'), action) AS action
            FROM admin_audit ORDER BY 1 LIMIT 300""")
    return [row['action'] for row in rows]
