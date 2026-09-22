"""Discover -> permission gate -> bounded download -> validate -> transaction -> durable receipt."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.catalog.multisource.sources import ADAPTERS, language_filter_codes
from app.catalog.multisource.store import PostgresStore, UpdateHeld
from app.catalog.multisource.transport import Downloader, DiskLimitError, write_json
from app.catalog.multisource.types import Candidate, ImportOptions
from app.catalog.policy import decide_license
from app.catalog.profiles import profile_languages, preferred_ids

LOG=logging.getLogger(__name__)
GOOD={'imported','updated','already_present','exact_duplicate','downloaded'}


@contextmanager
def process_lock(root: Path):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'multisource.lock').open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Another multi-source job is running in this cache') from exc
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)


def selected(candidate: Candidate,options: ImportOptions) -> bool:
    if options.profile=='none':return False
    if options.editions and candidate.key not in options.editions:return False
    languages=options.languages or profile_languages(options.profile)
    if languages is None or languages==():return True
    allowed=set().union(*(language_filter_codes(code) for code in languages))
    return candidate.metadata.language_code in allowed


def compact(candidate: Candidate) -> dict[str,Any]:
    m=candidate.metadata
    return {'key':candidate.key,'source':candidate.source,'id':m.translation_id,'language':m.language_code,
        'title':m.title,'source_url':candidate.url,'source_revision':candidate.revision,'numbering':candidate.numbering}


def finalize_status(items: list[dict], source_errors: list[dict], *, discover_only: bool=False) -> str:
    if discover_only:
        return 'partial' if source_errors or any(x['outcome'] in {'failed','not_found'} for x in items) else 'planned' if any(x['outcome']=='planned' for x in items) else 'failed'
    good=any(row['outcome'] in GOOD for row in items)
    problems=source_errors or any(row['outcome'] in {'failed','held_update','not_found'} for row in items)
    return 'failed' if not good else 'partial' if problems else 'succeeded'


async def acquire_corpus(root: Path,options: ImportOptions,*,store: Any=None,discover_only: bool=False,
                         downloader: Downloader|None=None,adapters: dict|None=None,refresh_catalog: bool=True,
                         progress_hook=None) -> dict[str,Any]:
    """The database-free download mode runs exactly the same parsers as production imports."""
    if not discover_only and not options.download_only and store is None:
        raise ValueError('An import needs a persistence store; use --download-only explicitly')
    started=time.monotonic()
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    report={'format_version':1,'application_version':'1.3.0','run_id':run_id,'status':'running',
        'started_at':datetime.now(timezone.utc).isoformat(),'options':asdict(options),
        'scope':'Configured sources and explicit open licenses only; not all Bibles worldwide',
        'items':[],'sources':{},'source_errors':[]}
    reports=root/'reports';reports.mkdir(parents=True,exist_ok=True)
    async def checkpoint():
        report['counts']=dict(Counter(x['outcome'] for x in report['items']))
        write_json(reports/(run_id+'.json'),report);write_json(reports/'latest.json',report)
        if progress_hook is not None:await progress_hook(report)
    if options.profile=='none':
        report['status']='skipped';await checkpoint();return report
    owned=downloader is None
    d=downloader or Downloader(root/'downloads',options)
    adapters=adapters or ADAPTERS
    sources={slug:adapters[slug](d) for slug in options.sources}
    catalog=[]
    try:
        for slug,source in sources.items():
            try:
                candidates=await source.catalog(refresh_catalog)
                report['sources'][slug]={'catalog_count':len(candidates),'notes':source.notes}
                for note in source.notes:
                    report['source_errors'].append({'source':slug,'stage':'catalog','error':note})
                catalog.extend(candidates)
            except Exception as exc:
                LOG.warning('Source catalog failed: %s (%s)',slug,type(exc).__name__)
                report['source_errors'].append({'source':slug,'stage':'catalog','error':f'{type(exc).__name__}: {exc}'[:1000]})
                report['sources'][slug]={'catalog_count':0,'status':'unavailable'}
            await checkpoint()
        # Reuse exact edition-level licensing metadata if BibleNLP is available. Otherwise
        # HelloAO performs independent eBible lookups instead of failing with GitHub.
        known={c.metadata.translation_id:c for c in catalog if c.source=='biblenlp'}
        from app.catalog.multisource.licenses import ebible_id
        allowed=[]
        for scan_index,candidate in enumerate(catalog):
            if scan_index % 50 == 0:
                report["permission_scan"]={"processed":scan_index,"catalog_size":len(catalog)}
                await checkpoint()
            row=compact(candidate)
            if not selected(candidate,options):
                report['items'].append({**row,'outcome':'not_selected'});continue
            try:
                counterpart=known.get(ebible_id(candidate.raw)) if candidate.source=='helloao' else None
                if counterpart and counterpart.metadata.language_code==candidate.metadata.language_code:
                    m=counterpart.metadata
                    candidate=replace(candidate,metadata=replace(candidate.metadata,license=m.license,
                        copyright_notice=m.copyright_notice,redistributable=m.redistributable,downloadable=m.downloadable),
                        evidence={**candidate.evidence,'license_basis':'exact eBible edition ID via BibleNLP metadata',
                            'license_reference_revision':counterpart.revision,**{k:v for k,v in counterpart.evidence.items() if k.startswith('license')}})
                else:
                    candidate=await sources[candidate.source].permission(candidate,refresh_catalog)
                decision=decide_license(candidate.metadata)
                row.update({'license':asdict(decision),'license_evidence':candidate.evidence})
                if not decision.allowed:
                    report['items'].append({**row,'outcome':'license_rejected'});continue
                allowed.append((candidate,row))
            except Exception as exc:
                report['items'].append({**row,'outcome':'failed','stage':'license','error':f'{type(exc).__name__}: {exc}'[:1000]})
        # Prefer complete advertised editions, and known preferred IDs within each language.
        allowed.sort(key=lambda pair:(options.sources.index(pair[0].source), pair[0].metadata.language_code,
            pair[0].metadata.translation_id.lower() not in preferred_ids(pair[0].metadata.language_code),
            -pair[0].metadata.total_verses, -int(pair[0].raw.get('totalNumberOfVerses',0)),pair[0].key))
        seen_keys={c.key for c in catalog}
        for key in options.editions:
            if key not in seen_keys:report['items'].append({'key':key,'outcome':'not_found'})
        successes=Counter()
        for position,(candidate,row) in enumerate(allowed,1):
            quota=(candidate.source,candidate.metadata.language_code)
            if options.profile!='all-open' and not options.editions and successes[quota]>=options.max_editions:
                report['items'].append({**row,'outcome':'quota_not_selected'});continue
            if discover_only:
                report['items'].append({**row,'outcome':'planned'});continue
            LOG.info('Corpus %d/%d: %s',position,len(allowed),candidate.key)
            report['current']=candidate.key;await checkpoint()
            try:
                try:
                    prepared=await sources[candidate.source].prepare(candidate,options.refresh)
                except (ValueError,UnicodeError):
                    if options.refresh:raise
                    # A mutable JSON URL may have an older cached generation than its catalog.
                    # Re-fetch ONCE; never repair missing verses from a different edition.
                    LOG.warning('Revalidating %s with fresh source files',candidate.key)
                    prepared=await sources[candidate.source].prepare(candidate,True)
                    row['validation_refresh_retry']=True
                if not decide_license(prepared.candidate.metadata).allowed:raise ValueError('License changed before write')
                result={'outcome':'downloaded'} if options.download_only else await store.save(prepared)
                result.update(row)
                result.update({'source_sha256':prepared.downloaded.sha256,'content_sha256':prepared.content_sha256,
                    'books':prepared.audit.books,'chapters':prepared.audit.chapters,'verses':prepared.audit.verses,
                    'visible_verses':prepared.audit.visible_verses,'coverage':prepared.audit.coverage,
                    'canonical_66_complete':prepared.audit.canonical_66_complete,'nt_complete':prepared.audit.nt_complete,
                    'audit':prepared.audit.as_dict(),'diagnostics':prepared.diagnostics})
                successes[quota]+=1
            except UpdateHeld as exc:
                result={**row,'outcome':'held_update','stage':'database','error':str(exc)}
            except DiskLimitError:
                raise  # A disk limit is global, not a reason to hammer the remaining sources.
            except Exception as exc:
                LOG.warning('Edition failed: %s (%s)',candidate.key,type(exc).__name__)
                result={**row,'outcome':'failed','stage':'download_validate_or_import','error':f'{type(exc).__name__}: {exc}'[:1000]}
            report['items'].append(result);await checkpoint()
        report['status']=finalize_status(report['items'],report['source_errors'],discover_only=discover_only)
        good=[x for x in report['items'] if x['outcome'] in GOOD]
        report['actual_languages']=sorted({x['language'] for x in good})
        requested=options.languages or profile_languages(options.profile)
        report['missing_requested_languages']=[code for code in requested or [] if not language_filter_codes(code).intersection(report['actual_languages'])]
        if report['missing_requested_languages'] and not discover_only and report['status']=='succeeded':report['status']='partial'
        report['complete_66_editions']=sum(bool(x.get('canonical_66_complete')) for x in good)
        report['nt_complete_editions']=sum(bool(x.get('nt_complete')) for x in good)
        report['database_written']=not discover_only and not options.download_only and any(x['outcome'] in {'imported','updated','exact_duplicate','already_present'} for x in good)
    except BaseException as exc:
        report['status']='interrupted' if isinstance(exc,(KeyboardInterrupt,asyncio.CancelledError)) else 'failed'
        report['fatal_error']=f'{type(exc).__name__}: {exc}'[:1000]
        raise
    finally:
        report.pop('current',None)
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        report['network']={'requests':d.requests,'downloaded_bytes':d.downloaded_bytes,'cache_hits':d.cache_hits,'resumes':d.resumes}
        report['report_file']=str(reports/(run_id+'.json'))
        try:
            await checkpoint()
        finally:
            if owned:await d.__aexit__()
    return report


async def run_multisource(settings,options: ImportOptions,*,maintenance_owned: bool=False,
                          refresh_catalog: bool=True) -> dict[str,Any]:
    from app.db import maintenance,normalize_asyncpg_dsn
    from app.catalog.importer import seed_books
    from app.services.locks import lock_key
    if options.profile=='none':return {'status':'skipped','reason':'BIBLE_PROFILE=none'}
    if not maintenance_owned and not options.download_only:
        async with maintenance(settings):
            return await run_multisource(settings,options,maintenance_owned=True,refresh_catalog=refresh_catalog)
    with process_lock(settings.source_cache_dir):
        if options.download_only:
            return await acquire_corpus(settings.source_cache_dir,options,refresh_catalog=refresh_catalog)
        import asyncpg
        connection=await asyncpg.connect(normalize_asyncpg_dsn(settings.database_url),timeout=30)
        run_id=None
        try:
            if not await connection.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('corpus-import',1)):
                raise RuntimeError('Another database corpus import is active')
            await seed_books(connection)
            run_id=await connection.fetchval("INSERT INTO import_runs(profile,source_name,status) VALUES($1,'multi-source','running') RETURNING id",options.profile)
            async def progress(report):
                counts=Counter(x['outcome'] for x in report['items'])
                status=report['status']
                db_status=status if status in {'running','succeeded','partial','failed'} else 'failed'
                await connection.execute('''UPDATE import_runs SET status=$2,selected_count=$3,imported_count=$4,
                    skipped_count=$5,failed_count=$6,details=$7::jsonb,
                    finished_at=CASE WHEN $2='running' THEN NULL ELSE now() END WHERE id=$1''',run_id,db_status,
                    sum(counts.values())-counts['not_selected']-counts['quota_not_selected'],
                    counts['imported']+counts['updated'],counts['already_present']+counts['exact_duplicate'],
                    counts['failed']+counts['held_update']+len(report['source_errors']),json.dumps(report,ensure_ascii=False,default=str))
            return await acquire_corpus(settings.source_cache_dir,options,
                store=PostgresStore(connection,batch_size=settings.import_batch_size,allow_replace=options.allow_replace),
                refresh_catalog=refresh_catalog,progress_hook=progress)
        except BaseException:
            if run_id is not None:
                await connection.execute("UPDATE import_runs SET status='failed',finished_at=now() WHERE id=$1 AND status='running'",run_id)
            raise
        finally:
            await connection.close()
