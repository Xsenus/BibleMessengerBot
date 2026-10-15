"""Исполнять настоящий shell-сценарий с контролируемыми внешними командами: оба сертификата без повторного jitter и безопасная проба без reload."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('with_domain', [False, True])
@pytest.mark.parametrize('dry_run', [False, True])
def test_owned_certificates_renew_without_double_jitter_or_dry_run_reload(tmp_path, with_domain, dry_run):
    root = Path(__file__).resolve().parents[1]
    shell = str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Git/bin/bash.exe') if os.name == 'nt' else shutil.which('bash')
    assert shell, 'Bash is required by the project maintenance scripts'
    cert_root = tmp_path / 'certs/max-edge'
    (cert_root / 'letsencrypt/live/bible-max-ip').mkdir(parents=True)
    (cert_root / 'letsencrypt/live/bible-max-ip/cert.pem').write_text('synthetic certificate; openssl is controlled')
    (cert_root / 'webroot').mkdir()
    if with_domain:
        (cert_root / 'letsencrypt/renewal').mkdir()
        (cert_root / 'letsencrypt/renewal/bible-admin-domain.conf').write_text('synthetic renewal configuration')
        (cert_root / 'letsencrypt/live/bible-admin-domain').mkdir()
        (cert_root / 'letsencrypt/live/bible-admin-domain/cert.pem').write_text('synthetic domain certificate')
    trace = tmp_path / 'commands.txt'
    environment = tmp_path / 'commands.sh'
    environment.write_text('''
record() { printf '%q ' "$@" >> "$RENEWAL_TRACE"; printf '\\n' >> "$RENEWAL_TRACE"; }
docker() {
    record docker "$@"
    if [[ "$1" == inspect && "$2" == --format ]]; then
        if [[ "$3" == *io.bible-messenger.role* ]]; then
            case "${@: -1}" in bible-max-acme) echo max-acme;; bible-max-edge) echo max-edge;; *) return 1;; esac
        else echo true; fi
    fi
}
curl() { record curl "$@"; }
flock() { record flock "$@"; }
openssl() { record openssl "$@"; }
''', encoding='utf-8')
    env = {**os.environ, 'MAX_CERT_PROJECT_DIR':tmp_path.as_posix(),
           'BASH_ENV':environment.as_posix(), 'RENEWAL_TRACE':trace.as_posix()}
    args = [shell, (root / 'max-cert-renew.sh').as_posix(), *(['--dry-run'] if dry_run else [])]
    completed = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    calls = [shlex.split(line) for line in trace.read_text().splitlines()]
    renewals = [call for call in calls if call[:2] == ['docker','run']]
    assert len(renewals) == (2 if with_domain else 1)
    assert [call[call.index('--cert-name') + 1] for call in renewals] == ['bible-max-ip', *(['bible-admin-domain'] if with_domain else [])]
    assert all('--no-random-sleep-on-renew' in call and ('--dry-run' in call) == dry_run for call in renewals)
    reloads = [call for call in calls if call[:3] == ['docker','exec','bible-max-edge']]
    assert reloads == ([] if dry_run else [['docker','exec','bible-max-edge','nginx','-t'], ['docker','exec','bible-max-edge','nginx','-s','reload']])
    assert not any('iptables' in call or 'xray' in call for call in calls)
