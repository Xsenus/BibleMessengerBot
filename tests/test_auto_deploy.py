"""Проверять настоящий Bash-сценарий на изолированном Git и управляемом Docker, без production и сетевых запросов."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import json

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELL = str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Git/bin/bash.exe') if os.name == 'nt' else shutil.which('bash')


@pytest.fixture
def deployment(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True, check=True).stdout.strip()

    git('init', '-b', 'main')
    git('config', 'user.name', 'Synthetic deployment test')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('config', 'core.autocrlf', 'false')
    (project / '.gitignore').write_text('.env\nruntime-evidence/\n', encoding='utf-8')
    (project / '.env').write_text('SYNTHETIC_ONLY=yes\n', encoding='utf-8')
    (project / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
    (project / 'backup.sh').write_text('''#!/usr/bin/env bash
echo backup >> "$DEPLOY_TRACE"
exit "${BACKUP_EXIT:-0}"
''', encoding='utf-8')
    (project / 'deploy').mkdir()
    (project / 'deploy/rolling-update.sh').write_text('''#!/usr/bin/env bash
echo activation >> "$DEPLOY_TRACE"
touch "$DEPLOY_ACTIVATED"
exit "${ACTIVATION_EXIT:-0}"
''', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'Synthetic previous release')
    previous = git('rev-parse', 'HEAD')
    (project / 'new-release.txt').write_text('Synthetic candidate release', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'Synthetic candidate release')
    target = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', target)
    git('switch', '--detach', previous)
    git('branch', '-f', 'main', previous)
    git('switch', 'main')
    trace = tmp_path / 'commands.txt'
    activated = tmp_path / 'activated'
    commands = tmp_path / 'commands.sh'
    commands.write_text('''
record() { printf '%q ' "$@" >> "$DEPLOY_TRACE"; printf '\\n' >> "$DEPLOY_TRACE"; }
id() { echo 0; }
flock() { record flock "$@"; }
python3() { "$DEPLOY_TEST_PYTHON" "$@"; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nsynthetic 99999999 0 %s 1%% /\\n' "${FREE_KIB:-8388608}"; }
git() { record git "$@"; if [[ "$1" != fetch ]]; then command git "$@"; fi; }
curl() { record curl "$@"; echo '{"status":"ready"}'; }
docker() {
  record docker "$@"
  if [[ "$1" == inspect ]]; then
    case "$3" in
      *Health.Status*) if [[ "${UNHEALTHY:-0}" == 1 && -f "$DEPLOY_ACTIVATED" ]]; then echo unhealthy; else echo healthy; fi;;
      *Config.Image*) echo bible-messenger-bot:synthetic-previous;;
      *org.opencontainers.image.revision*) if [[ "${WRONG_REVISION:-0}" == 1 ]]; then printf '%040d\\n' 0; else echo "$DEPLOY_TARGET_SHA"; fi;;
      *) echo sha256:synthetic-previous;;
    esac
    return
  fi
  [[ "$1" == compose ]] || return 0
  if [[ " $* " == *' ps --services '* ]]; then printf 'postgres\\nbot\\nworker\\nadmin\\n';
  elif [[ " $* " == *' ps -q '* ]]; then echo "container-${@: -1}";
  elif [[ " $* " == *' ps --format json '* ]]; then echo '{"synthetic":true}';
  elif [[ " $* " == *' exec -T postgres '* ]]; then
    if [[ "${SCHEMA_CHANGED:-0}" == 1 && -f "$DEPLOY_ACTIVATED" ]]; then echo new-schema; else echo previous-schema; fi
  elif [[ " $* " == *' port admin '* ]]; then echo 127.0.0.1:8080;
  elif [[ " $* " == *' up -d '* && " $* " == *'rollback-images.yml'* ]]; then rm -f "$DEPLOY_ACTIVATED";
  fi
}
''', encoding='utf-8')
    env = {**os.environ, 'BIBLE_DEPLOY_PROJECT_DIR':project.as_posix(),
           'BIBLE_DEPLOY_LOCK':(tmp_path / 'lock').as_posix(), 'BASH_ENV':commands.as_posix(),
           'DEPLOY_TRACE':trace.as_posix(), 'DEPLOY_ACTIVATED':activated.as_posix(),
           'DEPLOY_TEST_PYTHON':sys.executable, 'DEPLOY_TARGET_SHA':target,
           'BIBLE_DEPLOY_STATUS_WRITER':(ROOT / 'deploy/write-deployment-status.py').as_posix(),
           'GIT_CONFIG_NOSYSTEM':'1'}

    def run(**changes):
        assert SHELL, 'Bash is required by production deployment scripts'
        return subprocess.run([SHELL, (ROOT / 'deploy/auto-deploy.sh').as_posix(), 'run', target],
                              env={**env, **changes}, text=True, capture_output=True, timeout=30)

    return project, git, previous, target, trace, run


def test_success_deploys_exact_commit_and_preserves_configuration(deployment):
    project, git, previous, target, trace, run = deployment
    result = run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert git('rev-parse', 'HEAD') == target
    assert (project / '.env').read_text() == 'SYNTHETIC_ONLY=yes\n'
    assert (project / 'runtime-evidence/auto-deploy/deployed-commit').read_text().strip() == target
    status = json.loads((project / 'runtime-evidence/deploy-status/status.json').read_text())
    assert status['current']['commit'] == target and status['last_attempt']['status'] == 'succeeded'
    assert {row['name'] for row in status['current']['services']} == {'bot', 'worker', 'admin'}
    calls = trace.read_text().splitlines()
    assert calls.index('backup') < calls.index('activation')
    assert not any('down' in shlex.split(call) or 'restore' in shlex.split(call) for call in calls)


@pytest.mark.parametrize('failure', [{'ACTIVATION_EXIT':'17'}, {'UNHEALTHY':'1'}, {'WRONG_REVISION':'1'}])
def test_failed_update_restores_previous_images_and_checkout(deployment, failure):
    project, git, previous, target, trace, run = deployment
    result = run(**failure)
    assert result.returncode != 0
    assert git('rev-parse', 'HEAD') == previous
    assert 'schema unchanged' in result.stdout
    calls = trace.read_text()
    assert 'rollback-images.yml' in calls and '--no-deps' in calls
    assert not (project / 'runtime-evidence/auto-deploy/deployed-commit').exists()
    status = json.loads((project / 'runtime-evidence/deploy-status/status.json').read_text())
    assert status['last_attempt']['status'] == 'rolled_back' and status['current'] is None


def test_changed_schema_stops_workers_without_automatic_restore(deployment):
    project, git, previous, target, trace, run = deployment
    result = run(ACTIVATION_EXIT='17', SCHEMA_CHANGED='1')
    assert result.returncode != 0
    assert git('rev-parse', 'HEAD') == target
    assert 'operator intervention required' in result.stdout + result.stderr
    calls = [shlex.split(line) for line in trace.read_text().splitlines()]
    assert any('stop' in call and 'bot' in call and 'worker' in call for call in calls)
    assert not any('rollback-images.yml' in ' '.join(call) or 'pg_restore' in call for call in calls)


def test_backup_failure_never_changes_running_release(deployment):
    project, git, previous, target, trace, run = deployment
    result = run(BACKUP_EXIT='23')
    assert result.returncode == 23
    assert git('rev-parse', 'HEAD') == previous
    assert 'activation' not in trace.read_text()
    assert json.loads((project / 'runtime-evidence/deploy-status/status.json').read_text())['last_attempt']['status'] == 'failed'


def test_insufficient_disk_never_starts_backup_or_activation(deployment):
    project, git, previous, target, trace, run = deployment
    result = run(FREE_KIB='1024')
    assert result.returncode != 0 and git('rev-parse', 'HEAD') == previous
    assert 'backup' not in trace.read_text() and 'activation' not in trace.read_text()


def test_outdated_ci_commit_is_skipped(deployment):
    project, git, previous, target, trace, run = deployment
    git('update-ref', 'refs/remotes/origin/main', previous)
    result = run()
    assert result.returncode == 0 and 'SKIPPED' in result.stdout
    assert git('rev-parse', 'HEAD') == previous
    assert 'backup' not in trace.read_text() and 'activation' not in trace.read_text()


def test_dirty_checkout_is_preserved_and_refused(deployment):
    project, git, previous, target, trace, run = deployment
    (project / 'operator-notes.txt').write_text('Preserve these uncommitted operator notes')
    result = run()
    assert result.returncode != 0
    assert git('rev-parse', 'HEAD') == previous
    assert (project / 'operator-notes.txt').exists()
    assert not trace.exists() or 'activation' not in trace.read_text()


@pytest.mark.parametrize('command', ['main', 'a'*39, 'a'*40+';id', 'git-upload-pack /tmp/repo', ''])
def test_restricted_ssh_rejects_shell_and_invalid_commands(command):
    assert SHELL
    result = subprocess.run([SHELL, (ROOT / 'deploy/ssh-deploy.sh').as_posix()],
                            env={**os.environ, 'SSH_ORIGINAL_COMMAND':command}, text=True, capture_output=True, timeout=10)
    assert result.returncode == 64
