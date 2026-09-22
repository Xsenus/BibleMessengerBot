"""Shell orchestration contract tests with a fake Docker executable. Not Docker/PostgreSQL execution."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
FAKE=r'''#!/usr/bin/env -S python3 -S
import json,os,sys
args=sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'],'a') as f:f.write(json.dumps(args)+'\n')
if 'app.catalog.multisource.cli' in args:
    command=args[args.index('app.catalog.multisource.cli')+1]
    code=int(os.getenv('FAKE_'+command.upper()+'_EXIT','0'))
    print(json.dumps({'status':'partial' if code==2 else 'failed' if code else 'succeeded','synthetic':True}))
    sys.exit(code)
if 'app.cli' in args and 'audit' in args:
    code=int(os.getenv('FAKE_AUDIT_EXIT','0'))
    print(json.dumps({'status':'failed' if code else 'passed','synthetic':True}))
    sys.exit(code)
if 'ps' in args:
    print(os.getenv('FAKE_RUNNING','bot\nworker\nadmin'))
    sys.exit(0)
if 'exec' in args and 'postgres' in args and 'pg_dump' in ' '.join(args):
    print('SYNTHETIC BACKUP BYTES — NOT POSTGRESQL')
sys.exit(0)
'''


def invoke(tmp_path:Path,arguments=(),**settings):
    project=tmp_path/'project';project.mkdir()
    binpath=tmp_path/'bin';binpath.mkdir()
    for name in ('fill_database.sh','backup.sh'):
        shutil.copy2(ROOT/name,project/name)
    envfile=project/'.env';envfile.write_text('POSTGRES_PASSWORD=SYNTHETIC_NOT_A_REAL_PASSWORD\n')
    fake=binpath/'docker';fake.write_text(FAKE);fake.chmod(0o755)
    logfile=tmp_path/'docker-calls.jsonl'
    env={**os.environ,'PATH':str(binpath)+os.pathsep+os.environ['PATH'],'FAKE_DOCKER_LOG':str(logfile),**settings}
    proc=subprocess.run(['bash',str(project/'fill_database.sh'),*arguments],env=env,cwd=project,
                        text=True,capture_output=True,timeout=15)
    calls=[json.loads(line) for line in logfile.read_text().splitlines()]
    assert envfile.read_text()=='POSTGRES_PASSWORD=SYNTHETIC_NOT_A_REAL_PASSWORD\n'
    return proc,calls,project


def command_index(calls,word):
    return next(i for i,c in enumerate(calls) if word in c)


def cli(calls,command):
    return [c for c in calls if 'app.catalog.multisource.cli' in c and command in c]


def test_script_does_not_stop_services_on_download_failure(tmp_path):
    proc,calls,_=invoke(tmp_path,FAKE_DOWNLOAD_EXIT='1')
    assert proc.returncode==1 and not any('stop' in c for c in calls)
    assert not cli(calls,'import')


def test_script_orders_prefetch_backup_stop_import_audit_resume(tmp_path):
    proc,calls,project=invoke(tmp_path,('--refresh',),FAKE_RUNNING='bot')
    assert proc.returncode==0,proc.stderr
    fetch=command_index(calls,'download');stop=command_index(calls,'stop');imp=command_index(calls,'import')
    backup=next(i for i,c in enumerate(calls) if 'pg_dump' in ' '.join(c))
    assert fetch<backup<stop<imp
    request=cli(calls,'import')[0]
    assert '--cached-catalog' in request and '--refresh' not in request
    resumes=[c for c in calls if 'up' in c and '--no-deps' in c]
    assert resumes==[['compose','up','-d','--no-deps','bot']]
    assert list((project/'backups').glob('*.dump'))


def test_script_partial_exit_is_not_changed_to_success(tmp_path):
    proc,calls,_=invoke(tmp_path,FAKE_DOWNLOAD_EXIT='2',FAKE_IMPORT_EXIT='2')
    assert proc.returncode==2
    assert any('up' in c and '--no-deps' in c for c in calls)


def test_script_audit_failure_leaves_runtime_stopped(tmp_path):
    proc,calls,_=invoke(tmp_path,FAKE_AUDIT_EXIT='1')
    assert proc.returncode==1
    assert any('stop' in c for c in calls)
    assert not any('up' in c and '--no-deps' in c for c in calls)


def test_script_does_not_start_services_that_were_not_running(tmp_path):
    proc,calls,_=invoke(tmp_path,FAKE_RUNNING='')
    assert proc.returncode==0
    assert not any('stop' in c for c in calls)
    assert not any('up' in c and '--no-deps' in c for c in calls)


def test_script_rejects_invalid_config_before_acquisition(tmp_path):
    proc,calls,_=invoke(tmp_path,FAKE_CONFIG_EXIT='2')
    assert proc.returncode==2
    assert not cli(calls,'download') and not any('stop' in c for c in calls)


def test_explicit_download_action_has_no_database_operations(tmp_path):
    proc,calls,_=invoke(tmp_path,('download','--languages','ru,en'))
    assert proc.returncode==0
    assert not any(any(x in c for x in ['stop','postgres','import']) for c in calls)


def test_help_does_not_require_docker_env_or_touch_database(tmp_path):
    shutil.copy2(ROOT/'fill_database.sh',tmp_path/'fill_database.sh')
    proc=subprocess.run(['bash',str(tmp_path/'fill_database.sh'),'--help'],
        cwd=tmp_path,text=True,capture_output=True,timeout=5)
    assert proc.returncode==0
    assert 'Usage:' in proc.stdout
    assert not (tmp_path/'runtime-evidence').exists()
