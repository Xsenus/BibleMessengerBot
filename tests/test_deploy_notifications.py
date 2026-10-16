"""Проверять финальные уведомления без сети, боевых секретов и сообщений реальным пользователям."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import urllib.error

import pytest
import yaml

from app.services.release_info import FAILURES

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OLD = 'b' * 40
CONFIG = {'bot_token': '123:synthetic-token', 'chat_id': 123}
RUNTIME = [{'name': name, 'state': 'healthy', 'commit': SHA} for name in ['Telegram', 'MAX']]


@pytest.fixture
def notifier():
    spec = importlib.util.spec_from_file_location('deploy_notifier', ROOT / 'deploy/notify-deployment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failure_catalog_matches_admin():
    assert json.loads((ROOT / 'deploy/deployment-failures.json').read_text(encoding='utf-8')) == FAILURES


@pytest.fixture
def history(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], capture_output=True, text=True,
                              encoding='utf-8', check=True).stdout.strip()

    git('init', '-b', 'main')
    git('config', 'user.name', 'Synthetic notes test')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('config', 'core.autocrlf', 'false')
    (tmp_path / 'VERSION').write_text('1.20.0\n', encoding='utf-8')
    (tmp_path / 'CHANGELOG.md').write_text('# История\n\n- Старое изменение.\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'Synthetic old release')
    previous = git('rev-parse', 'HEAD')

    def release(changelog=None, title='Добавить поиск по стихам'):
        if changelog is not None:
            (tmp_path / 'CHANGELOG.md').write_text(changelog, encoding='utf-8')
        (tmp_path / 'VERSION').write_text('1.21.0\n', encoding='utf-8')
        git('add', '.')
        git('commit', '-m', title)
        return git('rev-parse', 'HEAD')

    return tmp_path, previous, git, release


def test_change_summary_uses_exact_changelog_delta_and_wrapped_entries(notifier, history):
    project, previous, git, release = history
    target = release('# История\n\n- Исправлен поиск.\n  По главе и стиху.\n- Добавлен голос.\n- Добавлен голос.\n\n- Старое изменение.\n')
    notes = notifier.changes(project, previous, target)
    assert notes[:2] == ['• Исправлен поиск. По главе и стиху.', '• Добавлен голос.']
    assert len(notes) == 3 and 'Старое изменение' not in '\n'.join(notes)
    assert notes[-1].endswith(previous + '...' + target)
    git('switch', '--detach', previous)
    assert notifier.changes(project, previous, target) == notes


def test_commit_titles_fallback_is_utf8_and_excludes_old_history(notifier, history):
    project, previous, git, release = history
    target = release()
    notes = notifier.changes(project, previous, target)
    assert notes[0] == '• Добавить поиск по стихам'
    assert len(notes) == 2 and 'Synthetic old release' not in '\n'.join(notes)


def test_summary_is_short_plain_text_and_links_to_remaining_changes(notifier, history, monkeypatch):
    project, previous, git, release = history
    entries = ['- **Пункт ' + str(i) + '** [поиска](https://example.invalid) \u202e' + 'подробности ' * 40 for i in range(8)]
    target = release('# История\n\n' + '\n'.join(entries) + '\n\n- Старое изменение.\n')
    notes = notifier.changes(project, previous, target)
    assert len([line for line in notes if line.startswith('• ')]) == 5
    assert max(len(line) for line in notes if line.startswith('• ')) <= 182
    assert 'Остальные изменения' in notes[-2]
    assert all(symbol not in '\n'.join(notes) for symbol in ['**', '[поиска]', '\u202e', 'example.invalid'])
    monkeypatch.setattr(notifier, 'version', lambda *args: '1.21.0')
    runtime = [dict(item, commit=target) for item in RUNTIME]
    text = notifier.message({'commit': target, 'previous_commit': previous, 'status': 'succeeded'}, project, runtime, FAILURES)
    assert 'Что обновилось:' in text and len(text) < 3500


@pytest.mark.parametrize('previous', [None, 'invalid', 'f' * 40])
def test_unconfirmed_base_never_lists_entire_history(notifier, history, previous):
    project, _, git, release = history
    target = release()
    text = '\n'.join(notifier.changes(project, previous, target))
    assert 'недоступен' in text and 'Добавить поиск' not in text and 'Synthetic old' not in text


def test_repeat_and_divergent_history_are_explicit(notifier, history):
    project, previous, git, release = history
    target = release()
    assert notifier.changes(project, target, target) == ['Новых изменений кода нет: повторная установка той же версии.']
    git('switch', '-c', 'synthetic-divergent', previous)
    divergent = release(title='Different synthetic branch')
    assert 'не удалось подтвердить диапазон' in '\n'.join(notifier.changes(project, target, divergent))


@pytest.mark.parametrize('status', ['failed', 'rolled_back', 'skipped', 'manual_intervention', 'succeeded'])
def test_uninstalled_changes_are_not_presented_as_deployed(notifier, monkeypatch, status):
    monkeypatch.setattr(notifier, 'version', lambda *args: '1.20.0')
    monkeypatch.setattr(notifier, 'changes', lambda *args: ['• Добавлен поиск.'])
    runtime = [dict(item, commit=OLD) for item in RUNTIME]
    text = notifier.message({'commit': SHA, 'previous_commit': OLD, 'status': status}, ROOT, runtime, FAILURES)
    assert 'Что обновилось:' not in text and 'установка изменений не подтверждена' in text
    assert '• Добавлен поиск.' in text and '1.20.0 · bbbbbbbb' in text


@pytest.fixture
def status_writer():
    spec = importlib.util.spec_from_file_location('changes_status_writer', ROOT / 'deploy/write-deployment-status.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_final_history_freezes_same_changes_as_telegram(notifier, status_writer, history, monkeypatch):
    project, previous, git, release = history
    target = release('# История\n\n- Исправлен поиск.\n\n- Старое изменение.\n')
    public = project / 'runtime-evidence/deploy-status'
    state = project / 'runtime-evidence/auto-deploy/current'
    state.mkdir(parents=True)
    (state / 'checked-services.jsonl').write_text(json.dumps({'name': 'bot', 'commit': target, 'health': 'healthy'}) + '\n')
    status_writer.publish(public, state, target, previous, 'succeeded', 'completed')
    data = json.loads((public / 'status.json').read_text(encoding='utf-8'))
    expected = notifier.change_summary(project, previous, target)
    assert data['history'][0]['changes'] == expected and expected['items'] == ['Исправлен поиск.']
    assert data['history'][0]['previous_commit'] == previous
    monkeypatch.setattr(status_writer, 'collect_changes', lambda *args: pytest.fail('Saved changes must not be recomputed'))
    status_writer.publish(public, state, target, previous, 'succeeded', 'completed')
    assert json.loads((public / 'status.json').read_text(encoding='utf-8'))['current']['changes'] == expected


def test_import_backfills_old_receipts_without_reading_logs_or_sending_messages(notifier, status_writer, history, monkeypatch):
    project, previous, git, release = history
    target = release()
    public, archive = project / 'runtime-evidence/deploy-status', project / 'runtime-evidence/auto-deploy'
    state = archive / ('20261015T020000Z-' + target[:12] + '-123')
    state.mkdir(parents=True)
    record = {'commit': target, 'previous_commit': previous, 'status': 'succeeded', 'stage': 'completed',
              'started_at': '2026-10-15T02:00:00+00:00', 'finished_at': '2026-10-15T03:00:00+00:00', 'services': []}
    raw = json.dumps(record).encode()
    (state / 'public-attempt.json').write_bytes(raw)
    (state / 'deploy.log').mkdir()
    (state / '.env').mkdir()
    status_writer.atomic_write(public, {'schema': 1, 'current': record, 'last_attempt': record,
                                      'history': [{key: value for key, value in record.items() if key != 'previous_commit'}]})
    assert status_writer.import_history(public, archive) == 1
    data = json.loads((public / 'status.json').read_text(encoding='utf-8'))
    expected = notifier.change_summary(project, previous, target)
    assert data['history'][0]['changes'] == data['current']['changes'] == data['last_attempt']['changes'] == expected
    assert data['history'][0]['previous_commit'] == previous
    assert (state / 'public-attempt.json').read_bytes() == raw and not (state / 'telegram-notification.json').exists()
    monkeypatch.setattr(status_writer, 'collect_changes', lambda *args: pytest.fail('Ready archive must remain frozen'))
    assert status_writer.import_history(public, archive) == 1
    assert json.loads((public / 'status.json').read_text(encoding='utf-8')) == data


def test_actual_version_and_health_are_independent_of_requested_commit(notifier, monkeypatch):
    monkeypatch.setattr(notifier, 'version', lambda project, sha: '1.20.0' if sha == OLD else '1.21.0')
    runtime = [dict(item, commit=OLD) for item in RUNTIME]
    receipt = {'commit': SHA, 'status': 'rolled_back', 'failure': {'code': 'backup_failed', 'exit_code': 23, 'detail': 'DO_NOT_LEAK'}}
    text = notifier.message(receipt, ROOT, runtime, FAILURES)
    assert '1.20.0 · bbbbbbbb' in text and '1.21.0' not in text
    assert 'Telegram: работает · bbbbbbbb' in text and 'MAX: работает' in text
    assert FAILURES['backup_failed'] in text and '23' in text and 'DO_NOT_LEAK' not in text
    assert 'Предыдущая версия восстановлена' in text and len(text) < 3500


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'manual_intervention', 'skipped'])
def test_partial_unknown_or_mixed_runtime_never_claims_full_install(notifier, status):
    runtime = [RUNTIME[0], {'name': 'MAX', 'state': 'unknown', 'commit': None}]
    text = notifier.message({'commit': SHA, 'status': status, 'failure': {'code': 'SENSITIVE_RAW_ERROR'}}, ROOT, runtime, FAILURES)
    assert 'Установленная версия: не подтверждена' in text and 'Установлено:' not in text
    assert 'MAX: проверить не удалось' in text and 'SENSITIVE_RAW_ERROR' not in text
    runtime[1].update(commit=OLD, state='unhealthy')
    text = notifier.message({'commit': SHA, 'status': status}, ROOT, runtime, FAILURES)
    assert 'разных сборках' in text and 'MAX: не прошёл healthcheck' in text


def test_runtime_inspection_uses_project_and_image_metadata_only(notifier, monkeypatch):
    calls = []

    def command(args):
        calls.append(args)
        if args[1] == 'ps':
            return 'abcdef123456' if args[-3].endswith('=bot') else ''
        return json.dumps({'Running': True, 'Health': {'Status': 'healthy'}}) + '|' + SHA

    monkeypatch.setattr(notifier, 'command', command)
    runtime = notifier.bots(Path('/synthetic/project'))
    assert runtime == [RUNTIME[0], {'name': 'MAX', 'state': 'absent', 'commit': None}]
    assert 'label=com.docker.compose.project.working_dir=' + str(Path('/synthetic/project')) in calls[0]
    assert 'org.opencontainers.image.revision' in calls[1][3]
    assert all('shell' not in arg and '.Env' not in arg for call in calls for arg in call)


def test_docker_failure_is_unknown_not_absent(notifier, monkeypatch):
    monkeypatch.setattr(notifier, 'command', lambda args: None)
    assert all(item['state'] == 'unknown' for item in notifier.bots(ROOT))


class Transport:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


@pytest.mark.parametrize(('response', 'error', 'expected'), [
    ({'ok': True, 'result': {'message_id': 42, 'chat': {'id': 123}}}, None, 'sent'),
    ({'ok': True, 'result': {'message_id': 42, 'chat': {'id': 999}}}, None, 'unknown'),
    ({'ok': False, 'description': 'SENSITIVE'}, None, 'failed'),
    ({}, None, 'unknown'),
    (None, TimeoutError('SENSITIVE'), 'unknown'),
    (None, urllib.error.URLError(socket.gaierror()), 'failed'),
    (None, urllib.error.HTTPError('SENSITIVE', 400, '', {}, None), 'failed'),
    (None, urllib.error.HTTPError('SENSITIVE', 503, '', {}, None), 'unknown'),
])
def test_delivery_receipts_and_ambiguous_network_results(notifier, response, error, expected):
    transport = Transport(response, error)
    result = notifier.send(CONFIG['bot_token'], 123, 'Synthetic notification', transport)
    assert result['state'] == expected and 'SENSITIVE' not in json.dumps(result)
    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert request.method == 'POST' and timeout == 15
    assert json.loads(request.data)['link_preview_options'] == {'is_disabled': True}


@pytest.fixture
def attempt(notifier, monkeypatch, tmp_path):
    (tmp_path / 'public-attempt.json').write_text(json.dumps({'commit': SHA, 'status': 'succeeded'}))
    monkeypatch.setattr(notifier, 'bots', lambda project: RUNTIME)
    monkeypatch.setattr(notifier, 'version', lambda project, sha: '1.20.0')
    return tmp_path


def test_persist_before_send_and_no_second_delivery(notifier, monkeypatch, attempt, capsys):
    calls = []

    def send(token, chat, text):
        assert json.loads((attempt / 'telegram-notification.json').read_text())['state'] == 'sending'
        calls.append(text)
        return {'state': 'sent', 'message_id': 42}

    monkeypatch.setattr(notifier, 'send', send)
    assert notifier.notify(ROOT, attempt, CONFIG, FAILURES)['state'] == 'sent'
    assert notifier.notify(ROOT, attempt, CONFIG, FAILURES)['state'] == 'sent'
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert CONFIG['bot_token'] not in output and 'BIBLE_DEPLOY_NOTICE=sent' in output
    report = next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('BIBLE_DEPLOY_REPORT='))
    assert json.loads(base64.b64decode(report))['text'] == calls[0]


@pytest.mark.parametrize('saved', ['{"state":"sending"}', 'broken json', '{"state":"unknown"}'])
def test_restart_never_repeats_uncertain_delivery(notifier, monkeypatch, attempt, saved):
    (attempt / 'telegram-notification.json').write_text(saved)
    monkeypatch.setattr(notifier, 'send', lambda *args: pytest.fail('duplicate delivery'))
    assert notifier.notify(ROOT, attempt, CONFIG, FAILURES)['state'] == 'unknown'


def test_post_send_receipt_disk_failure_suppresses_fallback_retry(notifier, monkeypatch, attempt, capsys):
    monkeypatch.setattr(notifier, 'send', lambda *args: {'state': 'sent', 'message_id': 42})
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == 'telegram-notification.json':
            raise OSError('SENSITIVE')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    assert notifier.notify(ROOT, attempt, CONFIG, FAILURES)['state'] == 'unknown'
    assert 'BIBLE_DEPLOY_NOTICE=unknown' in capsys.readouterr().out


def test_unconfigured_recipient_does_not_send(notifier, monkeypatch, attempt):
    monkeypatch.setattr(notifier, 'send', lambda *args: pytest.fail('unexpected delivery'))
    assert notifier.notify(ROOT, attempt, {}, FAILURES)['state'] == 'disabled'


@pytest.mark.parametrize(('ci', 'report', 'expected'), [
    ('failure', '', 'Обновление не запускалось'),
    ('cancelled', '', 'Обновление не запускалось'),
    ('success', '', 'Обновление могло продолжиться на сервере'),
    ('success', 'invalid!', 'Установленная версия и работа ботов пока неизвестны'),
    ('success', base64.b64encode(json.dumps({'text': 'Сбой backup aaaaaaaa'}).encode()).decode(), 'Сбой backup aaaaaaaa'),
    ('success', base64.b64encode(json.dumps({'text': 'Обновлено aaaaaaaa\nЧто обновилось:\n• Исправлен поиск.'}).encode()).decode(), 'Что обновилось:\n• Исправлен поиск.'),
])
def test_independent_github_fallback_reports_ci_ssh_and_host_failure(monkeypatch, capsys, ci, report, expected):
    workflow = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text(encoding='utf-8'))
    job = workflow['jobs']['notify-fallback']
    assert "!= 'sent'" in job['if'] and "!= 'unknown'" in job['if'] and 'always()' in job['if']
    assert 'head_repository.full_name == github.repository' in job['if']
    assert job['environment'] == 'production' and all('uses' not in step for step in job['steps'])
    source = job['steps'][0]['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    for key, value in dict(BOT_TOKEN=CONFIG['bot_token'], CHAT_ID='123', DEPLOY_SHA=SHA, CI_RESULT=ci,
                           CI_RUN='42', RUN_URL='https://github.com/synthetic/run/7', REPORT=report).items():
        monkeypatch.setenv(key, value)
    transport = Transport({'ok': True})
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: transport)
    exec(compile(source, 'synthetic-workflow-fallback', 'exec'), {})
    assert len(transport.calls) == 1
    text = json.loads(transport.calls[0][0].data)['text']
    assert expected in text and 'https://github.com/synthetic/run/7' in text
    assert CONFIG['bot_token'] not in text + capsys.readouterr().out


def test_github_fallback_network_failure_is_not_retried(monkeypatch, capsys):
    workflow = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text(encoding='utf-8'))
    source = workflow['jobs']['notify-fallback']['steps'][0]['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    for key, value in dict(BOT_TOKEN=CONFIG['bot_token'], CHAT_ID='123', DEPLOY_SHA=SHA, CI_RESULT='success',
                           CI_RUN='42', RUN_URL='https://github.com/synthetic/run/7', REPORT='').items():
        monkeypatch.setenv(key, value)
    transport = Transport(error=TimeoutError('SENSITIVE_TOKEN'))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: transport)
    exec(compile(source, 'synthetic-workflow-fallback', 'exec'), {})
    output = capsys.readouterr().out
    assert len(transport.calls) == 1 and '::warning::' in output and 'SENSITIVE_TOKEN' not in output
