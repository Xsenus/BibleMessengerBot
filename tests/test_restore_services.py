"""Restore service guards with fake Docker; SQL restore is not simulated as passed."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_rolling_update import FAKE

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX restore orchestration runs on Linux')
ROOT = Path(__file__).resolve().parents[1]


def invoke(tmp_path, *, start=False, fail=False):
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('restore.sh', 'backup.sh'):
        shutil.copyfile(ROOT / name, project / name)
    (project / 'input.dump').write_bytes(b'SYNTHETIC NOT A POSTGRESQL ARCHIVE')
    commands = tmp_path / 'bin'
    commands.mkdir()
    docker = commands / 'docker'
    docker.write_text(FAKE)
    docker.chmod(0o755)
    logfile = tmp_path / 'calls.jsonl'
    env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
               FAKE_LOG=str(logfile), FAKE_RESTORE='9' if fail else '0',
               FAKE_RUNNING='bot\nworker\nadmin\nmax-web\nmaxbot\nvkbot\npostgres')
    args = ['bash', str(project / 'restore.sh'), 'input.dump', '--confirm-replace-data']
    if start:
        args.append('--start-services')
    result = subprocess.run(args, cwd=project, env=env, text=True, capture_output=True, timeout=30)
    return result, [json.loads(line) for line in logfile.read_text().splitlines()], project


@pytest.mark.parametrize('start', [False, True])
def test_restore_stops_every_running_platform_and_resumes_only_on_request(tmp_path, start):
    result, calls, project = invoke(tmp_path, start=start)
    assert result.returncode == 0, result.stderr
    expected = ['bot', 'worker', 'admin', 'max-web', 'maxbot', 'vkbot']
    stop_index = next(i for i, call in enumerate(calls) if 'stop' in call)
    restore_index = next(i for i, call in enumerate(calls) if '--clean' in ' '.join(call))
    assert stop_index < restore_index
    assert calls[stop_index][-len(expected):] == expected
    assert (project / 'runtime-evidence' / 'restore-services.txt').read_text().splitlines() == expected
    assert any('migrate' in call for call in calls)
    assert not any('seed' in call for call in calls)
    resumed = [call for call in calls if 'up' in call and '--no-deps' in call]
    assert len(resumed) == int(start)
    if start:
        assert resumed[0][-len(expected):] == expected
        assert '--wait' in resumed[0]


def test_failed_restore_never_restarts_consumers_or_runs_followup_migrations(tmp_path):
    result, calls, _ = invoke(tmp_path, start=True, fail=True)
    assert result.returncode == 9
    assert any('stop' in call and 'max-web' in call and 'vkbot' in call for call in calls)
    assert not any('up' in call and '--no-deps' in call for call in calls)
    assert not any('migrate' in call for call in calls)
