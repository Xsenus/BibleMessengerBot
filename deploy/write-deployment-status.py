#!/usr/bin/env python3
"""Публиковать атомарную квитанцию деплоя без секретов в отдельный каталог для чтения админкой."""
import datetime
import hashlib
import json
import os
import re
import sys
import subprocess
import tempfile
from pathlib import Path

SERVICES = {'worker', 'composer', 'bot', 'admin', 'max-web', 'maxbot', 'vkbot', 'illustrator', 'speaker'}
STATUSES = {'running', 'succeeded', 'failed', 'rolled_back', 'manual_intervention', 'skipped'}
STAGES = {'checking', 'backup', 'updating', 'verifying', 'completed', 'rollback', 'stopped', 'skipped'}
REASONS = {'configuration_missing', 'dirty_checkout', 'unexpected_branch', 'fetch_failed', 'outdated_commit',
           'ancestry_failed', 'compose_invalid', 'disk_space', 'preflight_failed', 'backup_failed',
           'activation_failed', 'configuration_changed', 'postgres_changed', 'service_unhealthy',
           'revision_mismatch', 'migration_check_failed', 'admin_unready', 'verification_failed', 'interrupted',
           'legacy_unknown'}
HISTORY_LIMIT = 50
UTC = datetime.timezone(datetime.timedelta(0))  # Python 3.10 системного VPS.


def read(path):
    """Прочитать только небольшой JSON; отсутствующая или повреждённая квитанция не подменяется успехом."""
    try:
        if path.is_symlink():
            return {}
        with path.open('rb') as stream:
            payload = stream.read(262145)
        result = json.loads(payload) if len(payload) <= 262144 else None
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def moment(value):
    """Нормализовать только корректную дату с часовым поясом, не доверяя старой квитанции."""
    try:
        date = datetime.datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
        return date.astimezone(UTC).isoformat() if date and date.tzinfo else None
    except ValueError:
        return None


def change_details(value):
    """Сохранить только пять коротких пунктов и разрешённые признаки, без URL, путей и сырых ошибок."""
    if not isinstance(value, dict) or value.get('state') not in ('ready', 'unchanged', 'unavailable'):
        return None
    state = value['state']
    if state == 'ready':
        items = value.get('items')
        if (value.get('source') not in ('changelog', 'commits') or not isinstance(items, list) or not 1 <= len(items) <= 5
                or any(not isinstance(item, str) or not 0 < len(item) <= 180 or not item.isprintable() for item in items)):
            return None
        return {'state': state, 'source': value['source'], 'items': items, 'more': value.get('more') is True, 'reason': None}
    reason = value.get('reason')
    if state == 'unavailable' and reason not in ('previous_unknown', 'range_unavailable', 'description_unavailable'):
        return None
    return {'state': state, 'source': None, 'items': [], 'more': False, 'reason': reason if state == 'unavailable' else None}


def collect_changes(public_dir, previous, target):
    """Вызвать только режим списка у доверенного уведомителя; отказ Git не влияет на результат установки."""
    notifier = Path(__file__).with_name('notify-deployment.py')
    if not notifier.is_file():
        notifier = Path(__file__).with_name('bible-deploy-notify')
    try:
        result = subprocess.run([sys.executable, str(notifier), '--changes-json', str(public_dir.parent.parent), previous or '', target],
                                capture_output=True, text=True, encoding='utf-8', check=True, timeout=20)
        details = change_details(json.loads(result.stdout)) if len(result.stdout) <= 16384 else None
        if details:
            return details
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {'state': 'unavailable', 'source': None, 'items': [], 'more': False, 'reason': 'range_unavailable'}


def compact(data):
    """Отобрать небольшую запись истории без служб, путей и произвольного текста ошибок."""
    if (not isinstance(data, dict) or not isinstance(data.get('commit'), str)
            or not re.fullmatch('[0-9a-f]{40}', data['commit'])
            or not isinstance(data.get('status'), str) or data['status'] not in STATUSES):
        return None
    started = moment(data.get('started_at'))
    finished = moment(data.get('finished_at'))
    if not started or (data['status'] != 'running' and not finished):
        return None
    identity = hashlib.sha256((data['commit'] + ' ' + started).encode()).hexdigest()[:24]
    failure = data.get('failure')
    if (not isinstance(failure, dict) or not isinstance(failure.get('code'), str)
            or failure['code'] not in REASONS or data['status'] in {'running', 'succeeded'}):
        failure = None
    else:
        failure = {'code': failure['code'],
                   'stage': failure.get('stage') if isinstance(failure.get('stage'), str) and failure['stage'] in STAGES else None,
                   'exit_code': failure.get('exit_code') if type(failure.get('exit_code')) is int and 0 <= failure['exit_code'] <= 255 else None,
                   'service': failure.get('service') if isinstance(failure.get('service'), str) and failure['service'] in SERVICES else None}
    previous = data.get('previous_commit')
    return {'id': identity, 'commit': data['commit'],
            'previous_commit': previous if isinstance(previous, str) and re.fullmatch('[0-9a-f]{40}', previous) else None,
            'changes': change_details(data.get('changes')), 'status': data['status'],
            'stage': data.get('stage') if isinstance(data.get('stage'), str) and data['stage'] in STAGES else None,
            'started_at': started, 'finished_at': finished,
            'updated_at': moment(data.get('updated_at')), 'failure': failure,
            'legacy': data.get('legacy') is True}


def history(existing, *records):
    """Обновить одну попытку, сохранив повторные запуски того же SHA и максимум 50 последних попыток."""
    rows = existing.get('history', [])
    candidates = (rows[:HISTORY_LIMIT] if isinstance(rows, list) else []) + [existing.get('current'), existing.get('last_attempt'), *records]
    unique = {}
    for row in candidates:
        item = compact(row)
        if item:
            prior = unique.get(item['id'])
            if prior:
                if not item['previous_commit']:
                    item['previous_commit'] = prior['previous_commit']
                if not item['changes'] and item['previous_commit'] == prior['previous_commit']:
                    item['changes'] = prior['changes']
            unique[item['id']] = item
    return sorted(unique.values(), key=lambda row: row['started_at'], reverse=True)[:HISTORY_LIMIT]


def atomic_write(public_dir, document):
    """Атомарно заменить публичный JSON; каталог доступен контейнеру только для чтения."""
    public_dir.mkdir(parents=True, exist_ok=True)
    public_dir.chmod(0o755)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=public_dir, prefix='.status-', encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, public_dir / 'status.json')
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def publish(public_dir, state, target, previous, status, stage, reason='', failed_stage='', exit_code='', service=''):
    """Сохранить ход попытки и отдельно последний успешный релиз; не читать окружение, compose или логи."""
    if (not re.fullmatch('[0-9a-f]{40}', target) or not re.fullmatch('[0-9a-f]{40}', previous)
            or status not in STATUSES or stage not in STAGES):
        raise ValueError('invalid_status_arguments')
    now = datetime.datetime.now(UTC).isoformat()
    attempt_file = state / 'public-attempt.json'
    old_attempt = read(attempt_file)
    services = []
    checked = state / 'checked-services.jsonl'
    if checked.is_file() and checked.stat().st_size <= 16384:
        for line in checked.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            if (row.get('name') in SERVICES and row.get('health') == 'healthy'
                    and row.get('commit') == target):
                services.append({key: row[key] for key in ('name', 'health', 'commit')})
    latest = {'commit': target, 'previous_commit': previous, 'status': status, 'stage': stage,
              'started_at': old_attempt.get('started_at') or now, 'updated_at': now,
              'finished_at': None if status == 'running' else now, 'services': services}
    latest['changes'] = change_details(old_attempt.get('changes'))
    if status != 'running' and latest['changes'] is None:
        latest['changes'] = collect_changes(public_dir, previous, target)
    if reason:
        if reason not in REASONS or failed_stage not in STAGES or not exit_code.isdigit() or not 0 <= int(exit_code) <= 255 or (service and service not in SERVICES):
            raise ValueError('invalid_failure_arguments')
        latest['failure'] = {'code': reason, 'stage': failed_stage, 'exit_code': int(exit_code), 'service': service or None}
    if status == 'succeeded' and not services:
        raise ValueError('successful_deployment_requires_verified_services')
    attempt_file.write_text(json.dumps(latest), encoding='utf-8')
    destination = public_dir / 'status.json'
    existing = read(destination)
    current = latest if status == 'succeeded' else existing.get('current')
    document = {'schema': 1, 'current': current, 'last_attempt': latest, 'history': history(existing, latest)}
    atomic_write(public_dir, document)


def import_history(public_dir, archive):
    """Импортировать старые квитанции и маркеры успеха; не читать deploy.log, .env или compose-файлы."""
    existing = read(public_dir / 'status.json')
    records = []
    for folder in sorted(archive.iterdir(), reverse=True)[:200]:
        match = re.fullmatch(r'(\d{8}T\d{6}Z)-([0-9a-f]{12})-\d+', folder.name)
        if not match or not folder.is_dir() or folder.is_symlink():
            continue
        record = compact(read(folder / 'public-attempt.json'))
        if record and record['commit'].startswith(match[2]):
            records.append(record)
            continue
        marker, result = folder / 'deployed-commit', folder / 'exit-code'
        if not marker.is_file() or marker.is_symlink() or not result.is_file() or result.is_symlink():
            continue
        if marker.stat().st_size > 64 or result.stat().st_size > 8:
            continue
        sha = marker.read_text().strip()
        if not re.fullmatch('[0-9a-f]{40}', sha) or not sha.startswith(match[2]) or result.read_text().strip() != '0':
            continue
        try:
            started = datetime.datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
        finished = datetime.datetime.fromtimestamp(result.stat().st_mtime, UTC).isoformat()
        records.append({'commit': sha, 'status': 'succeeded', 'stage': 'completed', 'started_at': started,
                        'finished_at': finished, 'updated_at': finished, 'legacy': True})
    rows = history(existing, *records)
    for row in rows:
        if row['status'] != 'running' and (not row['changes'] or row['changes']['state'] == 'unavailable'):
            row['changes'] = collect_changes(public_dir, row['previous_commit'], row['commit'])
    document = {'schema': 1, 'current': existing.get('current'), 'last_attempt': existing.get('last_attempt'), 'history': rows}
    for key in ('current', 'last_attempt'):
        item = compact(document[key])
        enriched = next((row for row in rows if item and row['id'] == item['id']), None)
        if enriched and enriched['status'] != 'running':
            document[key] = {**document[key], 'previous_commit': enriched['previous_commit'], 'changes': enriched['changes']}
    atomic_write(public_dir, document)
    return len(document['history'])


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--import-history':
        print(import_history(Path(sys.argv[2]), Path(sys.argv[3])))
    elif len(sys.argv) in (7, 11):
        publish(Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:])
    else:
        raise SystemExit('Expected public directory, state directory, SHA, previous SHA, status, stage and optional failure fields')
