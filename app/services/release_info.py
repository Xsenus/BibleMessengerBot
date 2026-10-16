"""Безопасные сведения о сборке, результате установки и публичном запуске GitHub Actions."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
BUILD_FILE = ROOT / 'BUILD-METADATA.json'
STATUS_FILE = ROOT / 'deploy-status' / 'status.json'
REPOSITORY = 'https://github.com/Xsenus/BibleMessengerBot'
SERVICES = {'worker', 'composer', 'bot', 'admin', 'max-web', 'maxbot', 'vkbot', 'illustrator', 'speaker'}
STATES = {'running', 'succeeded', 'failed', 'rolled_back', 'manual_intervention', 'skipped'}
STAGES = {'checking', 'backup', 'updating', 'verifying', 'completed', 'rollback', 'stopped', 'skipped'}


def commit(value: Any) -> str | None:
    """Разрешить только полный SHA коммита, без произвольного текста и ссылок."""
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) else None


def moment(value: Any) -> str | None:
    """Привести корректный момент с часовым поясом к UTC; неизвестное время не придумывать."""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
        return parsed.astimezone(UTC).isoformat() if parsed is not None and parsed.tzinfo else None
    except ValueError:
        return None


def read_json(path: Path) -> dict | None:
    """Прочитать небольшой обычный JSON-файл; не читать ссылки, большие файлы и текст ошибок."""
    try:
        if path.is_symlink():
            return None
        with path.open('rb') as stream:
            payload = stream.read(131073)
        if len(payload) > 131072:
            return None
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def build_info() -> dict:
    """Вернуть версию и SHA из самого образа; отсутствие метаданных не заменять Git HEAD хоста."""
    data = read_json(BUILD_FILE) or {}
    try:
        raw_version = (ROOT / 'VERSION').read_text(encoding='utf-8')[:80].strip()
    except OSError:
        raw_version = ''
    version = raw_version if re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,39}', raw_version) else None
    sha = commit(data.get('commit'))
    return {'version': version, 'commit': sha, 'built_at': moment(data.get('built_at')),
            'commit_url': f'{REPOSITORY}/commit/{sha}' if sha else None}


def attempt(data: Any) -> dict | None:
    """Отдать только разрешённые поля квитанции, без журналов, путей, окружения и секретов."""
    if (not isinstance(data, dict) or not commit(data.get('commit'))
            or not isinstance(data.get('status'), str) or data['status'] not in STATES):
        return None
    started = moment(data.get('started_at'))
    finished = moment(data.get('finished_at'))
    if not started or (data['status'] != 'running' and not finished):
        return None
    services = []
    for item in data.get('services', []) if isinstance(data.get('services'), list) else []:
        if (isinstance(item, dict) and isinstance(item.get('name'), str) and item['name'] in SERVICES
                and item.get('health') == 'healthy' and commit(item.get('commit'))):
            services.append({'name': item['name'], 'health': 'healthy', 'commit': item['commit']})
    return {'commit': data['commit'], 'previous_commit': commit(data.get('previous_commit')),
            'status': data['status'], 'stage': data.get('stage') if isinstance(data.get('stage'), str) and data['stage'] in STAGES else None,
            'started_at': started, 'finished_at': finished, 'updated_at': moment(data.get('updated_at')),
            'services': services, 'commit_url': f'{REPOSITORY}/commit/{data["commit"]}'}


def server_info() -> dict:
    """Прочитать единственную публичную квитанцию из отдельного каталога, подключённого только для чтения."""
    data = read_json(STATUS_FILE)
    if not data or data.get('schema') != 1:
        return {'available': False, 'current': None, 'last_attempt': None, 'stale': False}
    current = attempt(data.get('current'))
    if current and current['status'] != 'succeeded':
        current = None
    latest = attempt(data.get('last_attempt'))
    updated = (latest['updated_at'] or latest['started_at']) if latest else None
    stale = bool(latest and latest['status'] == 'running' and updated
                 and (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds() > 5400)
    return {'available': bool(current or latest), 'current': current, 'last_attempt': latest, 'stale': stale}


class GithubDeployments:
    """Читать один публичный workflow с общим кэшем 120 секунд и явным признаком устаревших данных."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        """Создать кэш; управляемый transport нужен для проверок без внешней сети."""
        self.transport = transport
        self.lock = asyncio.Lock()
        self.next_refresh = 0.0
        self.result: dict = {'available': False, 'run': None, 'checked_at': None, 'stale': False}

    async def get(self) -> dict:
        """Не чаще одного запроса за две минуты; при ошибке сохранить прошлый результат как устаревший."""
        if time.monotonic() < self.next_refresh:
            return dict(self.result)
        async with self.lock:
            if time.monotonic() < self.next_refresh:
                return dict(self.result)
            try:
                async with asyncio.timeout(4), httpx.AsyncClient(
                        timeout=3, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                    response = await client.get(
                        'https://api.github.com/repos/Xsenus/BibleMessengerBot/actions/workflows/deploy.yml/runs',
                        params={'branch': 'main', 'per_page': 1}, headers={'Accept': 'application/vnd.github+json'})
                    response.raise_for_status()
                    if len(response.content) > 131072:
                        raise ValueError('oversized')
                    rows = response.json()['workflow_runs']
                    if not isinstance(rows, list):
                        raise ValueError('invalid')
                    run = self.public_run(rows[0]) if rows else None
                self.result = {'available': True, 'run': run,
                               'checked_at': datetime.now(UTC).isoformat(), 'stale': False}
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                self.result = {**self.result, 'available': False, 'stale': bool(self.result['checked_at'])}
            self.next_refresh = time.monotonic() + 120
            return dict(self.result)

    @staticmethod
    def public_run(data: Any) -> dict:
        """Проверить идентичность workflow и построить ссылку самостоятельно, не доверяя html_url из ответа."""
        if (not isinstance(data, dict) or data.get('head_branch') != 'main'
                or not commit(data.get('head_sha')) or type(data.get('id')) is not int
                or not 0 < data['id'] < 2**63 or data.get('event') != 'workflow_run'):
            raise ValueError('invalid_run')
        status = data.get('status')
        if status not in {'queued', 'in_progress', 'completed', 'requested', 'waiting', 'pending'}:
            raise ValueError('invalid_status')
        conclusion = data.get('conclusion')
        if conclusion not in {'success', 'failure', 'timed_out', 'cancelled', 'skipped', 'action_required', 'startup_failure', 'stale', 'neutral'}:
            conclusion = None
        return {'id': data['id'], 'commit': data['head_sha'], 'status': status, 'conclusion': conclusion,
                'started_at': moment(data.get('run_started_at')), 'updated_at': moment(data.get('updated_at')),
                'attempt': data.get('run_attempt') if type(data.get('run_attempt')) is int else None,
                'url': f'{REPOSITORY}/actions/runs/{data["id"]}'}
