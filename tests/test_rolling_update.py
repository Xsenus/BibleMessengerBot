"""Deployment orchestration with fake executables; no real containers or production DB."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX deployment tests run on Linux')
ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\n')
if 'config' in args:
    sys.exit(int(os.getenv('FAKE_CONFIG', '0')))
if 'build' in args:
    sys.exit(int(os.getenv('FAKE_BUILD', '0')))
if 'app.neural_audio_admin' in args:
    sys.exit(int(os.getenv('FAKE_MODELS', '0')))
if 'ps' in args and '--services' in args:
    print(os.getenv('FAKE_RUNNING', 'bot\nmax-web\npostgres\nprayer-ai'))
if 'app.cli' in args and 'migrate' in args:
    sys.exit(int(os.getenv('FAKE_CHECK', '3') if '--check' in args else os.getenv('FAKE_MIGRATE', '0')))
if 'exec' in args and 'pg_dump' in ' '.join(args):
    print('SYNTHETIC BACKUP, NOT A POSTGRESQL ARCHIVE')
    sys.exit(int(os.getenv('FAKE_BACKUP', '0')))
if 'pg_restore' in ' '.join(args) and '--clean' in ' '.join(args):
    sys.exit(int(os.getenv('FAKE_RESTORE', '0')))
if 'up' in args and args[-1] == os.getenv('FAKE_UP_FAIL'):
    sys.exit(7)
'''


def invoke(tmp_path, entrypoint='deploy/rolling-update.sh', **settings):
    project = tmp_path / 'project'
    (project / 'deploy').mkdir(parents=True)
    shutil.copy2(ROOT / 'deploy' / 'rolling-update.sh', project / 'deploy')
    shutil.copy2(ROOT / 'backup.sh', project)
    shutil.copy2(ROOT / 'update.sh', project)
    (project / '.env').write_text('POSTGRES_PASSWORD=SYNTHETIC\n')
    commands = tmp_path / 'bin'
    commands.mkdir()
    docker = commands / 'docker'
    docker.write_text(FAKE)
    docker.chmod(0o755)
    curl = commands / 'curl'
    curl.write_text('#!/usr/bin/env bash\nexit 0\n')
    curl.chmod(0o755)
    logfile = tmp_path / 'calls.jsonl'
    env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
               FAKE_LOG=str(logfile), **settings)
    result = subprocess.run(['bash', str(project / entrypoint), '--yes'],
                            cwd=project, env=env, text=True, capture_output=True, timeout=30)
    calls = [json.loads(line) for line in logfile.read_text().splitlines()]
    return result, calls


def test_migration_stops_max_web_and_restarts_only_previously_running_services(tmp_path):
    result, calls = invoke(tmp_path)
    assert result.returncode == 0, result.stderr
    stop = next(i for i, call in enumerate(calls) if 'stop' in call)
    dump = next(i for i, call in enumerate(calls) if 'pg_dump' in ' '.join(call))
    migrate = next(i for i, call in enumerate(calls) if 'migrate' in call and '--check' not in call)
    assert dump < stop < migrate
    assert calls[stop][-2:] == ['bot', 'max-web']
    restarted = [call[-1] for call in calls if 'up' in call]
    assert restarted == ['bot', 'max-web']
    assert all('--wait' in call for call in calls if 'up' in call)


def test_failed_migration_restores_existing_containers_including_max_web(tmp_path):
    result, calls = invoke(tmp_path, FAKE_MIGRATE='8')
    assert result.returncode == 8
    assert not any('up' in call for call in calls)
    assert next(call for call in calls if 'start' in call)[-2:] == ['bot', 'max-web']


@pytest.mark.parametrize('setting', ['FAKE_CONFIG', 'FAKE_BUILD', 'FAKE_MODELS', 'FAKE_BACKUP'])
def test_preflight_or_backup_failure_does_not_stop_services(tmp_path, setting):
    result, calls = invoke(tmp_path, **{setting: '4'})
    assert result.returncode == 4
    assert not any('stop' in call or 'up' in call or 'start' in call for call in calls)


def test_no_migrations_preserves_disabled_platforms(tmp_path):
    result, calls = invoke(tmp_path, FAKE_CHECK='0', FAKE_RUNNING='worker\npostgres')
    assert result.returncode == 0, result.stderr
    assert not any('stop' in call or 'start' in call for call in calls)
    assert [call[-1] for call in calls if 'up' in call] == ['worker']


def test_standard_update_entrypoint_keeps_max_web_in_migration_guard(tmp_path):
    result, calls = invoke(tmp_path, entrypoint='update.sh')
    assert result.returncode == 0, result.stderr
    assert next(call for call in calls if 'stop' in call)[-2:] == ['bot', 'max-web']
    assert [call[-1] for call in calls if 'up' in call] == ['bot', 'max-web']


def test_restart_failure_is_reported_and_remaining_containers_are_resumed(tmp_path):
    result, calls = invoke(tmp_path, FAKE_UP_FAIL='bot')
    assert result.returncode == 7
    assert next(call for call in calls if 'start' in call)[-2:] == ['bot', 'max-web']
