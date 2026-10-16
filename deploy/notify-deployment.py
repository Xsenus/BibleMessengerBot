#!/usr/bin/env python3
"""Однократно уведомлять личный чат о результате деплоя независимо от контейнеров и БД."""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import urllib.error
import urllib.request

STATUSES = {'succeeded', 'failed', 'rolled_back', 'manual_intervention', 'skipped'}


def read(path):
    """Прочитать ограниченный JSON; ссылки, произвольные исключения и большие файлы не публиковать."""
    try:
        if path.is_symlink():
            return {}
        with path.open('rb') as stream:
            raw = stream.read(131073)
        data = json.loads(raw) if len(raw) <= 131072 else None
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def command(args):
    """Выполнить ограниченную диагностическую команду без shell и без вывода stderr/секретов."""
    try:
        value = subprocess.run(args, capture_output=True, text=True, check=True, timeout=5).stdout
        return value if len(value) <= 16384 else None
    except (OSError, subprocess.SubprocessError):
        return None


def version(project, sha):
    """Читать VERSION именно коммита работающего образа, а не произвольного checkout."""
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        return None
    value = (command(['git', '-C', str(project), 'show', sha + ':VERSION']) or '').strip()
    return value if re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,39}', value) else None


def bots(project):
    """Проверить текущие контейнеры Telegram/MAX по рабочему каталогу Compose; другие проекты не трогать."""
    result = []
    for service, name in [('bot', 'Telegram'), ('maxbot', 'MAX')]:
        raw_ids = command(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project.working_dir=' + str(project),
                           '--filter', 'label=com.docker.compose.service=' + service, '--format', '{{.ID}}'])
        ids = (raw_ids or '').splitlines()
        item = {'name': name, 'state': 'unknown', 'commit': None}
        if len(ids) == 1 and re.fullmatch('[0-9a-f]{12,64}', ids[0]):
            raw = command(['docker', 'inspect', '--format', '{{json .State}}|{{index .Config.Labels "org.opencontainers.image.revision"}}', ids[0]])
            try:
                state, sha = (raw or '').strip().split('|', 1)
                state = json.loads(state)
                item['state'] = 'healthy' if state.get('Running') and state.get('Health', {}).get('Status') == 'healthy' else 'unhealthy'
                item['commit'] = sha if re.fullmatch('[0-9a-f]{40}', sha) else None
            except (ValueError, AttributeError):
                pass
        elif raw_ids is not None and not ids:
            item['state'] = 'absent'
        result.append(item)
    return result


def message(receipt, project, runtime, reasons):
    """Сформировать текст из разрешённых полей: итог, фактическая версия, healthcheck и исходная причина отказа."""
    sha = receipt.get('commit')
    status = receipt.get('status')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha) or not isinstance(status, str) or status not in STATUSES:
        raise ValueError('invalid_receipt')
    titles = {'succeeded': '✅ Автодеплой завершён', 'failed': '❌ Ошибка автодеплоя', 'rolled_back': '↩️ Автодеплой: выполнен откат',
              'manual_intervention': '🚨 Автодеплой: требуется вмешательство', 'skipped': '⏭ Автодеплой пропущен'}
    lines = [titles[status], 'BibleMessengerBot', 'Запрошен коммит: ' + sha[:8]]
    installed = {item['commit'] for item in runtime if item['commit']}
    if len(installed) == 1 and all(item['commit'] for item in runtime):
        active = next(iter(installed))
        lines.append('Установлено: ' + (version(project, active) or 'версия неизвестна') + ' · ' + active[:8])
    else:
        lines.append('Боты работают на разных сборках.' if len(installed) > 1 else 'Установленная версия: не подтверждена')
    names = {'healthy': 'работает', 'unhealthy': 'не прошёл healthcheck', 'absent': 'не запущен', 'unknown': 'проверить не удалось'}
    for item in runtime:
        lines.append(item['name'] + ': ' + names[item['state']] + (' · ' + item['commit'][:8] if item['commit'] else ''))
    lines.append('Проверка ботов: состояние процессов Docker, без тестовой рассылки.')
    failure = receipt.get('failure')
    if isinstance(failure, dict) and isinstance(failure.get('code'), str) and failure['code'] in reasons:
        lines.append('Причина: ' + reasons[failure['code']])
        if type(failure.get('exit_code')) is int:
            lines.append('Код выхода: ' + str(failure['exit_code']))
    elif status not in {'succeeded', 'skipped'}:
        lines.append('Причина: подробное подтверждение ошибки не получено, проверьте журнал деплоя.')
    if status == 'rolled_back':
        lines.append('Предыдущая версия восстановлена.')
    elif status == 'manual_intervention':
        lines.append('Безопасный откат не подтверждён; обработчики остановлены. Требуется проверка оператора.')
    elif status == 'failed' and receipt.get('stage') == 'rollback':
        lines.append('Откат не подтверждён. Требуется проверка оператора.')
    lines += ['', 'История и состояние: https://bible.blagodaty.ru/panel/',
              'Коммит: https://github.com/Xsenus/BibleMessengerBot/commit/' + sha]
    return '\n'.join(lines)


def send(token, chat_id, text, opener=None):
    """Одна попытка Bot API без сетевых повторов; таймаут/неоднозначный ответ не считается отказом доставки."""
    payload = json.dumps({'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}).encode()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=payload,
                                     headers={'Content-Type': 'application/json'}, method='POST')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            data = json.loads(response.read(65537))
        if (data.get('ok') is True and type(data.get('result', {}).get('message_id')) is int
                and data['result'].get('chat', {}).get('id') == chat_id):
            return {'state': 'sent', 'message_id': data['result']['message_id']}
        return {'state': 'failed'} if data.get('ok') is False else {'state': 'unknown'}
    except urllib.error.HTTPError as error:
        return {'state': 'failed'} if 400 <= error.code < 500 else {'state': 'unknown'}
    except urllib.error.URLError as error:
        return {'state': 'failed'} if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError)) else {'state': 'unknown'}
    except (OSError, ValueError, AttributeError):
        return {'state': 'unknown'}


def notify(project, state, config, reasons):
    """Зафиксировать отправку до запроса; повторный вызов не дублирует даже неоднозначно отправленное сообщение."""
    receipt = read(state / 'public-attempt.json')
    text = message(receipt, project, bots(project), reasons)
    print('BIBLE_DEPLOY_REPORT=' + base64.b64encode(json.dumps({'text': text}).encode()).decode(), flush=True)
    saved = state / 'telegram-notification.json'
    previous = read(saved)
    if saved.exists():
        result = previous if previous.get('state') in {'sent', 'unknown', 'failed'} else {'state': 'unknown'}
    elif (not isinstance(config.get('bot_token'), str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', config['bot_token'])
          or type(config.get('chat_id')) is not int or config['chat_id'] <= 0):
        result = {'state': 'disabled'}
    else:
        try:
            with saved.open('x', encoding='utf-8') as stream:
                saved.chmod(0o600)
                stream.write(json.dumps({'state': 'sending'}))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            print('BIBLE_DEPLOY_NOTICE=unknown', flush=True)
            return {'state': 'unknown'}
        result = send(config['bot_token'], config['chat_id'], text)
        result['checked_at'] = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).isoformat()
        try:
            saved.write_text(json.dumps(result), encoding='utf-8')
        except OSError:
            # Telegram уже мог принять сообщение: резервный канал не должен повторять отправку.
            result = {'state': 'unknown'}
    print('BIBLE_DEPLOY_NOTICE=' + result['state'], flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('project', type=Path)
    parser.add_argument('state', type=Path)
    parser.add_argument('--config', type=Path, default=Path(os.getenv('BIBLE_DEPLOY_NOTIFY_CONFIG', '/etc/bible-deploy-notify.json')))
    parser.add_argument('--messages', type=Path, default=Path(os.getenv('BIBLE_DEPLOY_NOTIFY_MESSAGES', '/usr/local/share/bible-messenger-bot/deployment-failures.json')))
    args = parser.parse_args()
    try:
        notify(args.project, args.state, read(args.config), read(args.messages))
    except Exception:
        # Не показывать URL Bot API, traceback и токен; сбой уведомления не откатывает исправный бот.
        print('BIBLE_DEPLOY_NOTICE=unavailable', flush=True)
