"""Безопасные сведения о сборке, результате установки и публичном запуске GitHub Actions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import deque
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
FAILURES = {
    'configuration_missing': 'На VPS отсутствует конфигурация проекта.',
    'dirty_checkout': 'В рабочем каталоге VPS есть незакоммиченные изменения.',
    'unexpected_branch': 'На VPS выбрана неожиданная рабочая ветка.',
    'fetch_failed': 'Не удалось получить main из GitHub.',
    'outdated_commit': 'Проверенный коммит уже заменён более новой main; установка пропущена.',
    'ancestry_failed': 'Текущую версию нельзя обновить до выбранного коммита без слияния.',
    'compose_invalid': 'Конфигурация Docker Compose не прошла проверку.',
    'disk_space': 'На VPS меньше необходимых 4 ГиБ свободного места.',
    'preflight_failed': 'Предварительная проверка VPS не пройдена.',
    'backup_failed': 'Создание или проверка резервной копии БД завершились ошибкой.',
    'activation_failed': 'Сборка или поочерёдное обновление служб завершились ошибкой.',
    'configuration_changed': 'После обновления изменился контрольный хеш конфигурации.',
    'postgres_changed': 'После обновления изменился контейнер PostgreSQL.',
    'service_unhealthy': 'Служба не прошла проверку работоспособности.',
    'revision_mismatch': 'Коммит образа службы не совпал с проверенным коммитом main.',
    'migration_check_failed': 'Проверка применённых миграций БД завершилась ошибкой.',
    'admin_unready': 'Админка не подтвердила готовность после обновления.',
    'verification_failed': 'Итоговая проверка установки завершилась ошибкой.',
    'interrupted': 'Процесс обновления получил сигнал прерывания.',
    'legacy_unknown': 'Причина ошибки не сохранялась этой версией автодеплоя.',
}


def failure_info(value: Any) -> dict | None:
    """Показать фиксированное описание кода ошибки; произвольный текст журналов никогда не возвращать."""
    if not isinstance(value, dict) or not isinstance(value.get('code'), str) or value['code'] not in FAILURES:
        return None
    return {'code': value['code'], 'message': FAILURES[value['code']],
            'stage': value.get('stage') if isinstance(value.get('stage'), str) and value['stage'] in STAGES else None,
            'exit_code': value.get('exit_code') if type(value.get('exit_code')) is int and 0 <= value['exit_code'] <= 255 else None,
            'service': value.get('service') if isinstance(value.get('service'), str) and value['service'] in SERVICES else None}


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
            payload = stream.read(262145)
        if len(payload) > 262144:
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


def change_info(value: Any, previous: str | None, target: str) -> dict | None:
    """Проверить сохранённый список; ссылку строить только из SHA, произвольные поля и URL не публиковать."""
    if not isinstance(value, dict) or value.get('state') not in ('ready', 'unchanged', 'unavailable'):
        return None
    state = value['state']
    items: list[str] = []
    source = None
    reason = None
    more = False
    if state == 'ready':
        raw = value.get('items')
        if (not previous or previous == target or value.get('source') not in ('changelog', 'commits')
                or not isinstance(raw, list) or not 1 <= len(raw) <= 5
                or any(not isinstance(item, str) or not 0 < len(item) <= 180 or not item.isprintable() for item in raw)):
            return None
        items, source, more = raw, value['source'], value.get('more') is True
    elif state == 'unchanged':
        if previous != target:
            return None
    else:
        reason = value.get('reason')
        if reason not in ('previous_unknown', 'range_unavailable', 'description_unavailable'):
            return None
    compare = f'{REPOSITORY}/compare/{previous}...{target}' if previous and previous != target and reason not in ('previous_unknown', 'range_unavailable') else None
    return {'state': state, 'source': source, 'items': items, 'more': more, 'reason': reason, 'compare_url': compare}


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
            'changes': change_info(data.get('changes'), commit(data.get('previous_commit')), data['commit']),
            'status': data['status'], 'stage': data.get('stage') if isinstance(data.get('stage'), str) and data['stage'] in STAGES else None,
            'started_at': started, 'finished_at': finished, 'updated_at': moment(data.get('updated_at')),
            'services': services, 'failure': failure_info(data.get('failure')) if data['status'] not in {'running', 'succeeded'} else None,
            'legacy': data.get('legacy') is True, 'commit_url': f'{REPOSITORY}/commit/{data["commit"]}'}


def history_records(data: dict) -> list[dict]:
    """Проверить и упорядочить максимум 50 попыток; обновления этапа не создают дубликатов."""
    rows = data.get('history', [])
    candidates = (rows[:50] if isinstance(rows, list) else []) + [data.get('current'), data.get('last_attempt')]
    unique: dict[str, dict] = {}
    for raw in candidates:
        item = attempt(raw)
        if item:
            item.pop('services')
            item['id'] = hashlib.sha256((item['commit'] + ' ' + item['started_at']).encode()).hexdigest()[:24]
            item['stale'] = bool(item['status'] == 'running' and (datetime.now(UTC) - datetime.fromisoformat(
                item['updated_at'] or item['started_at'])).total_seconds() > 5400)
            prior = unique.get(item['id'])
            if prior and not item['previous_commit']:
                item['previous_commit'] = prior['previous_commit']
            if prior and not item['changes'] and prior['previous_commit'] == item['previous_commit']:
                item['changes'] = prior['changes']
            unique[item['id']] = item
    return sorted(unique.values(), key=lambda row: row['started_at'], reverse=True)[:50]


def server_info() -> dict:
    """Прочитать единственную публичную квитанцию из отдельного каталога, подключённого только для чтения."""
    data = read_json(STATUS_FILE)
    if not data or data.get('schema') != 1:
        return {'available': False, 'current': None, 'last_attempt': None, 'stale': False, 'history': []}
    current = attempt(data.get('current'))
    if current and current['status'] != 'succeeded':
        current = None
    latest = attempt(data.get('last_attempt'))
    updated = (latest['updated_at'] or latest['started_at']) if latest else None
    stale = bool(latest and latest['status'] == 'running' and updated
                 and (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds() > 5400)
    history = history_records(data)
    return {'available': bool(current or latest or history), 'current': current, 'last_attempt': latest, 'stale': stale,
            'history': history}


class GithubDeployments:
    """Читать один публичный workflow с общим кэшем 120 секунд и явным признаком устаревших данных."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        """Создать кэш; управляемый transport нужен для проверок без внешней сети."""
        self.transport = transport
        self.lock = asyncio.Lock()
        self.next_refresh = 0.0
        self.requests: deque[float] = deque()
        self.attempts: dict[tuple[int, int], dict] = {}
        self.retry: dict[tuple[int, int], float] = {}
        self.result: dict = {'available': False, 'run': None, 'checked_at': None, 'stale': False,
                             'history': [], 'history_complete': False}

    async def request(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
        """Ограничить все запросы публичного API 50 в час на процесс; не пересылать ключ администратора."""
        now = time.monotonic()
        while self.requests and self.requests[0] <= now - 3600:
            self.requests.popleft()
        if len(self.requests) >= 50:
            raise ValueError('request_budget')
        self.requests.append(now)
        response = await client.get('https://api.github.com/repos/Xsenus/BibleMessengerBot/actions/' + path,
                                    params=params, headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
        if len(response.content) > 131072:
            raise ValueError('oversized')
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('invalid')
        return data

    async def older_attempts(self, client: httpx.AsyncClient, runs: list[dict]) -> tuple[list[dict], bool]:
        """Восстановить предыдущие попытки после повторного запуска, кэшируя завершённые результаты отдельно."""
        desired = [(row['id'], number) for row in runs for number in range(max(1, (row['attempt'] or 1) - 5), row['attempt'] or 1)]
        allowed = set(desired)
        self.attempts = {key: value for key, value in self.attempts.items() if key in allowed}
        self.retry = {key: value for key, value in self.retry.items() if key in allowed}
        # Не более одного дополнительного запроса за обновление; старые ошибки не вытесняют текущий статус.
        for key in desired:
            if key in self.attempts or self.retry.get(key, 0) > time.monotonic():
                continue
            try:
                async with asyncio.timeout(3):
                    item = self.public_run(await self.request(client, f'runs/{key[0]}/attempts/{key[1]}'))
                original = next(row for row in runs if row['id'] == key[0])
                if item['id'] != key[0] or item['attempt'] != key[1] or item['commit'] != original['commit'] or item['status'] != 'completed':
                    raise ValueError('attempt_identity')
                self.attempts[key] = item
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                self.retry[key] = time.monotonic() + 3600
            break
        return list(self.attempts.values()), len(self.attempts) == len(allowed)

    async def get(self) -> dict:
        """Обновить список раз в две минуты и одну старую попытку; при отказе пометить прошлые данные устаревшими."""
        if time.monotonic() < self.next_refresh:
            return dict(self.result)
        async with self.lock:
            if time.monotonic() < self.next_refresh:
                return dict(self.result)
            try:
                async with asyncio.timeout(8), httpx.AsyncClient(
                        timeout=3, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                    rows = (await self.request(client, 'workflows/deploy.yml/runs', {'branch': 'main', 'per_page': 10}))['workflow_runs']
                    if not isinstance(rows, list):
                        raise ValueError('invalid')
                    runs = [self.public_run(row) for row in rows[:10]]
                    older, complete = await self.older_attempts(client, runs)
                    history = sorted(runs + older, key=lambda row: (row['started_at'] or '', row['id'], row['attempt'] or 0), reverse=True)
                self.result = {'available': True, 'run': runs[0] if runs else None,
                               'checked_at': datetime.now(UTC).isoformat(), 'stale': False,
                               'history': history, 'history_complete': complete}
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
        attempt = data.get('run_attempt')
        attempt = attempt if type(attempt) is int and 1 <= attempt <= 10000 else None
        return {'id': data['id'], 'commit': data['head_sha'], 'status': status, 'conclusion': conclusion,
                'started_at': moment(data.get('run_started_at')), 'updated_at': moment(data.get('updated_at')),
                'attempt': attempt,
                'url': f'{REPOSITORY}/actions/runs/{data["id"]}',
                'attempt_url': f'{REPOSITORY}/actions/runs/{data["id"]}/attempts/{attempt}' if attempt else None}
