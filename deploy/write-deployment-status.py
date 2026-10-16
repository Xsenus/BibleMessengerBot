#!/usr/bin/env python3
"""Публиковать атомарную квитанцию деплоя без секретов в отдельный каталог для чтения админкой."""
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SERVICES = {'worker', 'composer', 'bot', 'admin', 'max-web', 'maxbot', 'vkbot', 'illustrator', 'speaker'}
STATUSES = {'running', 'succeeded', 'failed', 'rolled_back', 'manual_intervention', 'skipped'}
STAGES = {'checking', 'backup', 'updating', 'verifying', 'completed', 'rollback', 'stopped', 'skipped'}


def read(path):
    """Прочитать только небольшой JSON; отсутствующая или повреждённая квитанция не подменяется успехом."""
    try:
        if path.is_symlink():
            return {}
        with path.open('rb') as stream:
            payload = stream.read(131073)
        result = json.loads(payload) if len(payload) <= 131072 else None
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def publish(public_dir, state, target, previous, status, stage):
    """Сохранить ход попытки и отдельно последний успешный релиз; не читать окружение, compose или логи."""
    if (not re.fullmatch('[0-9a-f]{40}', target) or not re.fullmatch('[0-9a-f]{40}', previous)
            or status not in STATUSES or stage not in STAGES):
        raise ValueError('invalid_status_arguments')
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
    if status == 'succeeded' and not services:
        raise ValueError('successful_deployment_requires_verified_services')
    attempt_file.write_text(json.dumps(latest), encoding='utf-8')
    public_dir.mkdir(parents=True, exist_ok=True)
    public_dir.chmod(0o755)
    destination = public_dir / 'status.json'
    existing = read(destination)
    current = latest if status == 'succeeded' else existing.get('current')
    document = {'schema': 1, 'current': current, 'last_attempt': latest}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=public_dir, prefix='.status-', encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    if len(sys.argv) != 7:
        raise SystemExit('Expected public directory, state directory, target SHA, previous SHA, status and stage')
    publish(Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:])
