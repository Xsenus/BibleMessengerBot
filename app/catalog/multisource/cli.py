"""Multi-source command line. Discovery/download/status do not import asyncpg or require a bot token.

Exit status: 0 succeeded/planned/skipped, 2 partial, 1 failed, 130 interrupted.
Full JSON receipts are always persisted under SOURCE_CACHE_DIR/reports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict,replace
from pathlib import Path
from typing import Any

from app.catalog.multisource.runner import acquire_corpus,process_lock,run_multisource
from app.catalog.multisource.transport import write_json
from app.catalog.multisource.types import ImportOptions


def csv(value: str) -> tuple[str,...]:
    return tuple(x.strip() for x in value.split(',') if x.strip())


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description='Download and validate licensed editions from independent resources; atomically import into BibleMessenger PostgreSQL')
    p.add_argument('command',choices=('discover','download','import','status','config'))
    p.add_argument('--sources',type=csv,help='biblenlp,getbible,helloao (subset and order)')
    p.add_argument('--profile',choices=('all-open','core','extended','none'))
    p.add_argument('--languages',type=csv,help='ISO codes, e.g. ru,en,es. Empty means no additional filter')
    p.add_argument('--editions',type=csv,help='Exact source:id keys, e.g. biblenlp:russyn,helloao:BSB')
    p.add_argument('--max-editions',type=int,help='Successful editions per language PER SOURCE; ignored by all-open')
    p.add_argument('--refresh',action='store_true',help='Re-download completed corpus files too; default refreshes catalog only')
    p.add_argument('--cached-catalog',action='store_true',help='Reuse SHA-checked catalog snapshot from an earlier download phase')
    p.add_argument('--replace-existing',action='store_true',help='Allow changed text ONLY when it has no subscriptions, progress, defaults, delivery history or aliases')
    p.add_argument('--cache-dir',type=Path)
    p.add_argument('--output',type=Path,help='Additional full report path (inside container for Docker commands)')
    p.add_argument('--full-report',action='store_true',help='Print all entries rather than a summary')
    return p


def report_summary(report: dict[str,Any]) -> dict[str,Any]:
    fields=('application_version','run_id','status','started_at','finished_at','elapsed_seconds','options','scope',
        'counts','sources','source_errors','actual_languages','missing_requested_languages','complete_66_editions',
        'nt_complete_editions','database_written','network','report_file','fatal_error','validation_error','exception_type','cache_dir')
    return {key:report[key] for key in fields if key in report}


def options_from_args(args: argparse.Namespace) -> ImportOptions:
    changes={name:getattr(args,name) for name in ('sources','profile','languages','editions','max_editions') if getattr(args,name) is not None}
    changes.update(refresh=args.refresh,download_only=args.command=='download',allow_replace=args.replace_existing)
    return ImportOptions.from_env(**changes)


async def run(args: argparse.Namespace) -> dict[str,Any]:
    import os
    root=args.cache_dir or Path(os.getenv('SOURCE_CACHE_DIR','/app/cache'))
    if args.command=='status':
        path=root/'reports/latest.json'
        if not path.is_file():
            return {'status':'failed','fatal_error':'No import receipt yet','report_file':str(path)}
        return json.loads(path.read_text(encoding='utf-8'))
    options=options_from_args(args)
    if args.command=='config':
        return {'status':'planned','options':asdict(options),'cache_dir':str(root)}
    if args.command in {'discover','download'}:
        with process_lock(root):
            return await acquire_corpus(root,options,discover_only=args.command=='discover',refresh_catalog=not args.cached_catalog)
    # A live write requires the real driver. No mock database fallback exists.
    from app.config import Settings
    from app.db import wait_for_database,maintenance,apply_schema
    from app.services.seed import seed_static_content
    from app.services.locks import lock_key
    settings=replace(Settings.from_env(require_bot_token=False),source_cache_dir=root,bible_profile=options.profile)
    await wait_for_database(settings)
    async with maintenance(settings) as connection:
        await apply_schema(settings)
        async with connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock($1)',lock_key('static-seed',1))
            await seed_static_content(connection)
        return await run_multisource(settings,options,maintenance_owned=True,refresh_catalog=not args.cached_catalog)


def exit_code(report: dict[str,Any]) -> int:
    return 0 if report.get('status') in {'succeeded','planned','skipped'} else 2 if report.get('status')=='partial' else 1


def main() -> None:
    from app.logging import configure_logging
    import sys
    configure_logging(stream=sys.stderr)
    args=parser().parse_args()
    try:
        report=asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning('Interrupted. Check the saved receipt; partial downloads can be resumed.')
        raise SystemExit(130) from None
    except Exception as exc:
        # Do not print connection exceptions, DSNs, environment secrets or a traceback by default.
        report={'status':'failed','fatal_error':f'{type(exc).__name__}: command failed; see cache/reports/latest.json and stderr progress',
                'exception_type':type(exc).__name__}
        if isinstance(exc,(ValueError,ImportError)):
            report['validation_error']=str(exc)[:1000]
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        write_json(args.output,report)
    print(json.dumps(report if args.full_report else report_summary(report),ensure_ascii=False,indent=2,default=str))
    raise SystemExit(exit_code(report))


if __name__=='__main__':
    main()
