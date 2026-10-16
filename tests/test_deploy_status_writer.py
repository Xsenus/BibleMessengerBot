"""Проверять атомарную публичную квитанцию настоящим процессом Python, без Docker и production."""
import json
import os
from pathlib import Path
import subprocess
import sys


WRITER = Path(__file__).resolve().parents[1] / 'deploy/write-deployment-status.py'
SHA = 'a'*40
OLD = 'b'*40


def publish(tmp_path, target=SHA, status='running', stage='checking'):
    public = tmp_path / 'public'
    state = tmp_path / target
    state.mkdir(exist_ok=True)
    result = subprocess.run([sys.executable,str(WRITER),str(public),str(state),target,OLD,status,stage],
                            text=True,capture_output=True,timeout=10)
    return result, public, state


def test_failed_attempt_retains_successful_release_and_omits_private_files(tmp_path):
    result, public, state = publish(tmp_path)
    assert result.returncode == 0
    before = json.loads((public / 'status.json').read_text())
    assert before['current'] is None and before['last_attempt']['finished_at'] is None
    (state / 'environment.sha256').write_text('private do not expose')
    (state / 'checked-services.jsonl').write_text(json.dumps({'name':'bot','commit':SHA,'health':'healthy','token':'private do not expose'})+'\n')
    result, _, _ = publish(tmp_path,status='succeeded',stage='completed')
    assert result.returncode == 0
    success = json.loads((public / 'status.json').read_text())
    assert success['current']['commit']==SHA and success['last_attempt']['started_at']==before['last_attempt']['started_at']
    result, _, _ = publish(tmp_path,target='c'*40,status='rolled_back',stage='rollback')
    assert result.returncode == 0
    current = json.loads((public / 'status.json').read_text())
    assert current['current']==success['current'] and current['last_attempt']['status']=='rolled_back'
    assert 'private do not expose' not in (public / 'status.json').read_text()
    assert not list(public.glob('.status-*'))
    if os.name != 'nt':
        assert (public / 'status.json').stat().st_mode & 0o777 == 0o644
        assert public.stat().st_mode & 0o777 == 0o755


def test_missing_verification_cannot_publish_success(tmp_path):
    result, public, state = publish(tmp_path,status='succeeded',stage='completed')
    assert result.returncode != 0 and not (public / 'status.json').exists()


def test_invalid_sha_cannot_publish_status(tmp_path):
    result, public, state = publish(tmp_path,target='main',status='failed',stage='checking')
    assert result.returncode != 0 and not public.exists()
