#!/usr/bin/env python3
"""Record reproducible OFFLINE checks, with unexecuted integration work kept explicit.

Running this script updates evidence/reports. It does not sign the package, run
Docker, silently install dependencies, or turn skipped tests into passed tests.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
import yaml

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence'
EXCLUDED = {'.git','__pycache__','.pytest_cache','.ruff_cache','.venv','venv','backups','cache','runtime-evidence'}
TOKEN = re.compile(r'\b\d{6,14}:[A-Za-z0-9_-]{30,}\b')


def project_files() -> list[Path]:
    """Select distributed source files, not runtime state or bytecode."""
    return sorted(p for p in ROOT.rglob('*') if p.is_file()
                  and not set(p.relative_to(ROOT).parts).intersection(EXCLUDED))


def command(name: str, args: list[str]) -> dict[str,Any]:
    """Execute once, capture the full log and record a genuine exit code."""
    try:
        result = subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,timeout=180,check=False)
        code,output = result.returncode,result.stdout
    except (OSError,subprocess.TimeoutExpired) as exc:
        code,output = 127,repr(exc)
    path = EVIDENCE / f'{name}.txt'
    path.write_text(output,encoding='utf-8')
    return {'name':name,'status':'passed' if code==0 else 'failed',
            'command':args,'returncode':code,'log':str(path.relative_to(ROOT))}


def main() -> int:
    """Run offline validation and explicitly report missing integration prerequisites."""
    EVIDENCE.mkdir(exist_ok=True)
    checks = [command('COMPILE',[sys.executable,'-m','compileall','-q','app','tests','scripts']),
              command('TESTS',[sys.executable,'-m','pytest','-o','addopts=','-q','-rs',
                     '--junitxml=evidence/TESTS.xml','-p','no:cacheprovider'])]
    tree = ElementTree.parse(EVIDENCE/'TESTS.xml')
    cases = tree.findall('.//testcase')
    skipped = [{'name':case.attrib.get('name'),'reason':case.find('skipped').attrib.get('message',''), 'detail':case.find('skipped').text}
               for case in cases if case.find('skipped') is not None]
    failed = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
    tests = {'passed':len(cases)-len(skipped)-failed,'failed':failed,
             'skipped_collection_units_or_cases':len(skipped),'skipped':skipped}
    for path in sorted([*ROOT.glob('*.sh'),*ROOT.glob('scripts/*.sh')]):
        checks.append(command('SHELL-'+path.stem,['bash','-n',str(path.relative_to(ROOT))]))
    structured_errors = []
    parsed_files = []
    for path in project_files():
        if 'history' in path.parts or path.name.startswith('RELEASE-AUDIT'):
            continue
        try:
            if path.suffix=='.json':json.loads(path.read_text(encoding='utf-8'))
            elif path.suffix in {'.yml','.yaml'}:yaml.safe_load(path.read_text(encoding='utf-8'))
            else:continue
            parsed_files.append(str(path.relative_to(ROOT)))
        except (ValueError,yaml.YAMLError) as exc:
            structured_errors.append(f'{path.relative_to(ROOT)}: {exc}')
    checks.append({'name':'JSON_YAML_PARSE_ONLY','status':'failed' if structured_errors else 'passed',
                   'files':parsed_files,'errors':structured_errors,
                   'limitation':'This is not docker compose config/build or execution of SQL.'})
    unsafe = []
    for path in project_files():
        rel = str(path.relative_to(ROOT))
        if path.name=='.env' or path.is_symlink():unsafe.append(rel)
        if path.suffix in {'.bundle','.zip','.pyc'} or 'history' in path.parts:continue
        if path.stat().st_size<5_000_000:
            text=path.read_text(encoding='utf-8',errors='ignore')
            if TOKEN.search(text) or re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',text):
                unsafe.append(rel)
    checks.append({'name':'BASIC_SECRET_AND_PATH_SCAN','status':'failed' if unsafe else 'passed',
                   'matches':unsafe,'limitation':'A basic pattern scan, not a full security audit.'})
    required=['README.md','VERSION','Dockerfile','docker-compose.yml','.env.example','install.sh',
              'sql/schema.sql','sql/migrations/002_hardening.sql','sql/migrations/003_multisource.sql','fill_database.sh','app/catalog/multisource/cli.py','docs/MULTISOURCE_IMPORT_RU.md','app/bootstrap.py','app/bot/main.py',
              'app/worker/main.py','app/web/main.py','docs/LIMITATIONS.md','docs/LANGUAGE-MATRIX.md',
              'tests/test_postgres_integration.py','tests/test_aiogram_contracts.py']
    missing=[p for p in required if not (ROOT/p).is_file()]
    checks.append({'name':'REQUIRED_FILES','status':'failed' if missing else 'passed','missing':missing})
    env={'python':sys.version,'platform':platform.platform(),
         'modules':{m:importlib.util.find_spec(m) is not None for m in ['pytest','aiogram','asyncpg','yaml','httpx']},
         'executables':{m:shutil.which(m) for m in ['docker','postgres','initdb','ruff','git','bash']}}
    (EVIDENCE/'ENVIRONMENT.json').write_text(json.dumps(env,indent=2)+'\n')
    not_executed=[
        'Docker image build and docker compose config/runtime on target VPS.',
        'Full authentic Bible download/import; no complete edition is physically bundled.',
        'Real Telegram Bot API requests, channel/group/private delivery and scheduled live acceptance.',
        'Real backup/restore, sustained load/soak tests, independent textual/native-language review.',
        'Ruff, dependency vulnerability audit and fully hash-locked reproducible image build.'
    ]
    if not env['modules']['aiogram']:
        not_executed.insert(0,'Actual aiogram model/authorization integration suite: missing library.')
    if not env['modules']['asyncpg'] or os.getenv('RUN_DB_TESTS')!='1':
        not_executed.insert(0,'Actual PostgreSQL/asyncpg integration: missing asyncpg or RUN_DB_TESTS is not enabled.')
    ok=all(c['status']=='passed' for c in checks)
    profiles=json.loads((ROOT/'data/language_profiles.json').read_text())
    report={'project':'BibleMessengerBot','version':(ROOT/'VERSION').read_text().strip(),
            'generated_at_machine_clock':datetime.now(timezone.utc).isoformat(),'release_context_date':'2026-09-22',
            'status':'OFFLINE_PASS_LIVE_UNVERIFIED' if ok else 'OFFLINE_FAILED',
            'tests':tests,'checks':checks,'environment':env,
            'ui_catalogs':len(list((ROOT/'locales').glob('*.json'))),
            'extended_profile_language_codes':len(profiles['extended']),
            'default_profile':'all-open','default_language_limit':None,'source_adapters':['biblenlp','getbible','helloao'],
            'network_source_tests':'synthetic httpx streams; not real downloads',
            'shell_tests':'fake Docker executable; not actual containers',
            'bundled_authentic_complete_editions':0,'prebuilt_database_dump_included':False,
            'not_executed':not_executed,
            'runtime_gate':'install.sh executes installed-library and disposable PostgreSQL tests, then real import+database audit before starting services; not run here.'}
    (ROOT/'RELEASE-AUDIT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# Проверка BibleMessengerBot '+report['version'],'',
           '**Статус: '+report['status']+'**. Это отчёт офлайн-проверок, а не приёмка production.',
           '',f"Выполнено тестов успешно: **{tests['passed']}**. Ошибок: **{tests['failed']}**. Пропущено единиц сбора/тестов: **{len(skipped)}**.",
           'Пропуски перечислены ниже и в XML/JSON; они не посчитаны как прошедшие. ' + '; '.join(x['name'] or '' for x in skipped),
           '', '## Выполненные проверки','']
    lines += [f"- {c['status'].upper()} — {c['name']}" for c in checks]
    lines += ['', 'Полные команды, причины пропусков и результаты: RELEASE-AUDIT.json, evidence/TESTS.txt и TESTS.xml. Разбор YAML не является запуском Docker Compose. Компиляция Python не проверяет отсутствующие зависимости или SQL на сервере.',
              '', '## Не выполнено','']+[f'- {x}' for x in not_executed]
    lines += ['', '## Фактическое наполнение', '',
              f"Каталогов UI: {report['ui_catalogs']}. Источников: 3. Профиль all-open не ограничен списком из 56 языков, но фильтрует лицензии и формат. Полностью скачанных изданий в ZIP: **0**. Готового дампа нет.",
              '', 'Установщик должен выполнить реальные интеграционные тесты, импорт и аудит на VPS. До их успешного завершения публикации не запускаются. Это дополнительная проверка на сервере, а не уже выполненная здесь работа. Гарантии отсутствия всех ошибок нет.']
    (ROOT/'RELEASE-AUDIT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'status':report['status'],'tests':tests,'report':'RELEASE-AUDIT.json'},ensure_ascii=False,indent=2))
    return 0 if ok else 1

if __name__=='__main__':
    raise SystemExit(main())
