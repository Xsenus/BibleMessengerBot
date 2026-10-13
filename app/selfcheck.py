"""Живая проверка настроенных служб: `python -m app.selfcheck [--rotate-secrets]`.

Проверяет то, что реально нужно для работы: база и миграции, ключ шифрования, токены Telegram/MAX/ВКонтакте, видимость
сети для внешних служб (через настроенную маршрутизацию), локальные программы (ffmpeg, tesseract). Печатает JSON,
код возврата 1, если есть критичные ошибки.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Any

import asyncpg
import httpx

from app.config import Settings
from app.db import migration_status, normalize_asyncpg_dsn
from app.services import secrets_box


def item(name: str, ok: bool | None, detail: str = '', critical: bool = False) -> dict[str, Any]:
    """Результат одной проверки: ok=None — не настроено (не ошибка)."""
    return dict(name=name, status='skipped' if ok is None else 'ok' if ok else ('fail' if critical else 'warn'), detail=detail)


async def fetch_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
        response = await http.get(url)
        return response.json()


async def check_telegram(token: str) -> dict:
    try:
        data = await fetch_json(f'https://api.telegram.org/bot{token}/getMe')
        return item('telegram', bool(data.get('ok')), data.get('result', {}).get('username', ''), critical=True)
    except Exception as error:
        return item('telegram', False, type(error).__name__, critical=True)


async def check_vk(token: str) -> dict:
    try:
        data = await fetch_json(f'https://api.vk.com/method/groups.getById?access_token={token}&v=5.199')
        return item('vk', 'response' in data, str(data.get('error', {}).get('error_msg', '')))
    except Exception as error:
        return item('vk', False, type(error).__name__)


async def check_max(token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
            response = await http.get('https://platform-api.max.ru/me', headers={'Authorization': token})
        return item('max', response.status_code == 200, f'HTTP {response.status_code}')
    except Exception as error:
        return item('max', False, type(error).__name__)


async def rotate_secrets(connection: Any) -> int:
    """Перешифровать все сохранённые секреты основным ключом; возвращает число обновлённых значений."""
    changed = 0
    for table, column in (('image_custom_providers', 'api_key_enc'), ('net_proxies', 'url_enc')):
        try:
            rows = await connection.fetch(f'SELECT id,{column} AS value FROM {table}')   # noqa: S608 (имена из списка выше)
        except asyncpg.UndefinedTableError:
            continue
        for row in rows:
            if row['value'] and secrets_box.needs_rotation(row['value']):
                await connection.execute(f'UPDATE {table} SET {column}=$2 WHERE id=$1', row['id'],   # noqa: S608
                                         secrets_box.encrypt(secrets_box.decrypt(row['value'])))
                changed += 1
    return changed


async def run(rotate: bool = False) -> dict[str, Any]:
    settings = Settings.from_env(require_bot_token=False)
    results: list[dict[str, Any]] = []
    try:
        connection = await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url), timeout=15)
    except Exception as error:
        return dict(status='failed', checks=[item('database', False, type(error).__name__, critical=True)])
    try:
        status = await migration_status(settings)
        results.append(item('database', True, 'connected', critical=True))
        results.append(item('migrations', status['status'] == 'ok', f"pending={status['pending']} modified={status['modified']}", critical=True))
        if rotate:
            results.append(item('secrets_rotation', True, f'updated: {await rotate_secrets(connection)}'))
    finally:
        await connection.close()
    results.append(item('secrets_key', bool(os.getenv('SECRETS_KEY', '').strip()),
                        'задайте отдельный SECRETS_KEY (24+ символа): сейчас используется ADMIN_API_KEY' if not os.getenv('SECRETS_KEY', '').strip() else 'dedicated'))
    for binary in ('ffmpeg', 'tesseract'):
        results.append(item(binary, shutil.which(binary) is not None, shutil.which(binary) or 'не найден'))
    if os.getenv('BOT_TOKEN') not in {None, '', 'replace_me'}:
        results.append(await check_telegram(os.environ['BOT_TOKEN']))
    if os.getenv('MAX_BOT_TOKEN'):
        results.append(await check_max(os.environ['MAX_BOT_TOKEN']))
    if os.getenv('VK_BOT_TOKEN'):
        results.append(await check_vk(os.environ['VK_BOT_TOKEN']))
    failed = any(r['status'] == 'fail' for r in results)
    return dict(status='failed' if failed else 'warn' if any(r['status'] == 'warn' for r in results) else 'ok', checks=results)


def main() -> None:
    report = asyncio.run(run('--rotate-secrets' in sys.argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report['status'] == 'failed' else 0)


if __name__ == '__main__':
    main()
