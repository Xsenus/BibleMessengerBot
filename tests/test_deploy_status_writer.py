"""Проверять атомарную публичную квитанцию настоящим процессом Python, без Docker и production."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


WRITER = Path(__file__).resolve().parents[1] / 'deploy/write-deployment-status.py'
SHA = 'a'*40
OLD = 'b'*40


def writer_module():
    spec = importlib.util.spec_from_file_location('deployment_writer', WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_history_updates_attempt_and_keeps_retries_and_original_failure(tmp_path):
    writer = writer_module()
    public = tmp_path / 'public'
    first, retry = tmp_path / 'first', tmp_path / 'retry'
    first.mkdir()
    retry.mkdir()
    writer.publish(public, first, SHA, OLD, 'running', 'backup')
    writer.publish(public, first, SHA, OLD, 'failed', 'backup', 'backup_failed', 'backup', '23', '')
    writer.publish(public, retry, SHA, OLD, 'running', 'updating')
    writer.publish(public, retry, SHA, OLD, 'rolled_back', 'rollback', 'activation_failed', 'updating', '17', '')
    data = json.loads((public / 'status.json').read_text())
    assert len(data['history']) == 2 and len({row['id'] for row in data['history']}) == 2
    assert data['history'][0]['failure'] == {'code':'activation_failed','stage':'updating','exit_code':17,'service':None}
    assert data['history'][1]['failure']['code'] == 'backup_failed'
    assert data['current'] is None


def test_history_is_bounded_and_does_not_copy_unknown_fields(tmp_path):
    writer = writer_module()
    existing = {'history':[]}
    for number in range(70):
        row = {'commit':SHA,'status':'failed','stage':'backup','started_at':f'2026-10-16T02:{number//60:02d}:{number%60:02d}+00:00',
               'finished_at':'2026-10-16T04:00:00+00:00','password':'private',
               'failure':{'code':'backup_failed','stage':'backup','exit_code':1,'message':'private','service':'private'}}
        existing['history'] = writer.history(existing, row)
    assert len(existing['history']) == 50
    assert existing['history'][0]['started_at'] == '2026-10-16T02:01:09+00:00'
    assert 'private' not in json.dumps(existing)
    assert writer.compact({'commit':SHA,'status':[]}) is None


def test_import_preserves_current_release_and_recovers_legacy_without_reading_logs(tmp_path):
    writer = writer_module()
    public, archive = tmp_path / 'public', tmp_path / 'archive'
    state = tmp_path / 'current'
    state.mkdir()
    writer.publish(public, state, SHA, OLD, 'running', 'checking')
    current = json.loads((public / 'status.json').read_text())
    legacy = archive / ('20261015T020000Z-'+OLD[:12]+'-123')
    legacy.mkdir(parents=True)
    (legacy / 'deployed-commit').write_text(OLD+'\n')
    (legacy / 'exit-code').write_text('0\n')
    # Каталог вместо файла доказывает отсутствие чтения приватного журнала.
    (legacy / 'deploy.log').mkdir()
    (legacy / '.env').write_text('password=never expose')
    assert writer.import_history(public, archive) == 2
    assert writer.import_history(public, archive) == 2
    data = json.loads((public / 'status.json').read_text())
    assert data['current'] == current['current'] and data['last_attempt'] == current['last_attempt']
    old = next(row for row in data['history'] if row['commit']==OLD)
    assert old['legacy'] and old['status']=='succeeded'
    assert 'never expose' not in json.dumps(data)


def test_writer_and_api_allow_the_same_failure_codes():
    from app.services.release_info import FAILURES
    assert set(FAILURES) == writer_module().REASONS
