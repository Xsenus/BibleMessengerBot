"""End-to-end acquisition tests with synthetic HTTP responses, NOT real external Bible data."""
from __future__ import annotations
import csv
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.catalog.multisource.runner import acquire_corpus,process_lock,selected,finalize_status
from app.catalog.multisource.cli import parser,run,exit_code,options_from_args
from app.catalog.multisource.sources import BibleNlp,GetBible,HelloAO
from app.catalog.multisource.types import ImportOptions
from app.catalog.multisource.store import UpdateHeld
from app.catalog.multisource.transport import DiskLimitError
from app.services import bible
from tests.multisource_fixtures import hello_fixture,get_fixture,downloader,response,options

REV='a'*40

def bible_csv(identifier='sample'):
    fields=['languageCode','translationId','languageName','languageNameInEnglish','title','description',
        'Redistributable','Copyright','publicationURL','OTbooks','OTchapters','OTverses',
        'NTbooks','NTchapters','NTverses','DCbooks','DCchapters','DCverses','textDirection','downloadable','shortTitle','script','sourceDate']
    raw={key:'' for key in fields}
    raw.update(languageCode='eng',translationId=identifier,languageName='Fixture English',languageNameInEnglish='Fixture English',
        title='SYNTHETIC',Redistributable='True',Copyright='Public Domain',OTbooks='1',OTchapters='1',OTverses='2',
        textDirection='ltr',downloadable='True')
    f=io.StringIO();w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerow(raw)
    return f.getvalue()

def license_tsv(identifier='sample'):
    return 'ID\tLicence Type\tCC Licence Link\n'+identifier+'\tpublic domain\t\n'

def handler_for_all(tmp_path):
    hc,hp,hx,hi=hello_fixture(tmp_path)
    gc,gp,gx,gi=get_fixture(tmp_path)
    hr={**hc.raw,'website':'https://ebible.org/find/show.php?id=sample'}
    routes={
        f'https://raw.githubusercontent.com/BibleNLP/ebible/{REV}/metadata/translations.csv':response(content=bible_csv().encode()),
        f'https://raw.githubusercontent.com/BibleNLP/ebible/{REV}/metadata/licences.tsv':response(content=license_tsv().encode()),
        f'https://raw.githubusercontent.com/BibleNLP/ebible/{REV}/metadata/vref.txt':response(content=b'GEN 1:1\nGEN 1:2\n'),
        f'https://raw.githubusercontent.com/BibleNLP/ebible/{REV}/corpus/eng-sample.txt':response(content=b'SYNTHETIC FIRST\nSYNTHETIC SECOND\n'),
        'https://api.getbible.net/v2/translations.json':response({'sample':gc.raw}),
        gc.url:response(gx),'https://api.getbible.net/v2/sample/books.json':response(gi),
        'https://bible.helloao.org/api/available_translations.json':response({'translations':[hr]}),
        hc.url:response(hx),'https://bible.helloao.org/api/sample/books.json':response(hi),
        'https://ebible.org/find/show.php?id=sample':response(content=b'<p>Public Domain</p>'),
    }
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        if str(req.url) not in routes:
            raise AssertionError('Unexpected network request '+str(req.url))
        return routes[str(req.url)]
    return handler,seen

async def test_three_real_adapters_process_bytes_independently(tmp_path,monkeypatch):
    monkeypatch.setenv('BIBLENLP_REVISION',REV)
    handler,seen=handler_for_all(tmp_path);d=downloader(tmp_path,handler)
    try:
        report=await acquire_corpus(tmp_path/'state',options(download_only=True),downloader=d)
        assert report['status']=='succeeded',report
        assert report['counts']=={'downloaded':3}
        assert report['actual_languages']==['eng']
        assert not report['database_written']
        assert report['complete_66_editions']==0
        assert {x['source'] for x in report['items']}=={'biblenlp','getbible','helloao'}
        assert {x['verses'] for x in report['items']}=={2}
        saved=json.loads((tmp_path/'state/reports/latest.json').read_text())
        assert saved['run_id']==report['run_id']
        # Exact ID/license metadata is reused, without guessing across edition names.
        assert 'https://ebible.org/find/show.php?id=sample' not in seen
    finally:await d.client.aclose()

async def test_helloao_does_not_require_github_for_license_lookup(tmp_path):
    handler,seen=handler_for_all(tmp_path);d=downloader(tmp_path,handler)
    try:
        report=await acquire_corpus(tmp_path/'state',options(sources=('helloao',),download_only=True),downloader=d)
        assert report['status']=='succeeded',report
        assert 'https://ebible.org/find/show.php?id=sample' in seen
        assert report['items'][0]['license_evidence']['license_sha256']
    finally:await d.client.aclose()

async def test_unavailable_source_does_not_prevent_other_imports(tmp_path,monkeypatch):
    monkeypatch.setenv('BIBLENLP_REVISION',REV)
    base,seen=handler_for_all(tmp_path)
    def handler(req):
        return response(status=503,content=b'Unavailable',headers={'Retry-After':'0'}) if req.url.host=='raw.githubusercontent.com' else base(req)
    d=downloader(tmp_path,handler)
    try:
        report=await acquire_corpus(tmp_path/'state',options(sources=('biblenlp','getbible'),download_only=True),downloader=d)
        assert report['status']=='partial' and report['counts']['downloaded']==1
        assert report['source_errors'][0]['source']=='biblenlp'
    finally:await d.client.aclose()

async def test_unknown_license_never_downloads_translation(tmp_path):
    c,p,x,inv=get_fixture(tmp_path)
    raw={**c.raw,'distribution_license':'MIT repository software'}
    calls=[]
    def handler(req):
        calls.append(str(req.url))
        assert str(req.url).endswith('/translations.json')
        return response({'sample':raw})
    d=downloader(tmp_path,handler)
    try:
        report=await acquire_corpus(tmp_path/'state',options(sources=('getbible',),download_only=True),downloader=d)
        assert report['status']=='failed' and report['counts']['license_rejected']==1
        assert len(calls)==1
    finally:await d.client.aclose()

async def test_latest_revision_resolved_and_pinned_for_whole_run(tmp_path,monkeypatch):
    monkeypatch.setenv('BIBLENLP_REVISION','latest')
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        if str(req.url).endswith('/ebible'):return response({'default_branch':'main'})
        if str(req.url).endswith('/commits/main'):return response({'sha':REV})
        if str(req.url).endswith('translations.csv'):return response(content=bible_csv().encode())
        if str(req.url).endswith('licences.tsv'):return response(content=license_tsv().encode())
        raise AssertionError(str(req.url))
    d=downloader(tmp_path,handler)
    try:
        source=BibleNlp(d);items=await source.catalog(True)
        assert source.revision==REV and not source.notes and items[0].revision==REV
        assert len(seen)==4 and all(REV in url for url in seen[2:])
    finally:await d.client.aclose()

class FixtureSource:
    notes=[]
    candidates=[]
    failures={}
    def __init__(self,d):pass
    async def catalog(self,refresh):return self.candidates
    async def permission(self,c,refresh):return c
    async def prepare(self,c,refresh):
        if c.key in self.failures:raise self.failures[c.key]
        from app.catalog.multisource.parsers import parse_helloao
        return parse_helloao(c,self.path,self.payload,self.inventory)

async def test_write_failures_are_recorded_not_claimed_as_success(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path)
    class Source(FixtureSource):candidates=[c];path=p;payload=x;inventory=inv
    store=SimpleNamespace(save=AsyncMock(side_effect=RuntimeError('synthetic transaction failure')))
    d=downloader(tmp_path,lambda req:(_ for _ in ()).throw(AssertionError('network not expected')))
    try:
        r=await acquire_corpus(tmp_path/'state',options(sources=('helloao',)),store=store,downloader=d,adapters={'helloao':Source})
        assert r['status']=='failed' and r['counts']['failed']==1 and not r['database_written']
        assert store.save.await_count==1
    finally:await d.client.aclose()

async def test_changed_edition_is_held_not_overwritten_by_runner(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path)
    class Source(FixtureSource):candidates=[c];path=p;payload=x;inventory=inv
    store=SimpleNamespace(save=AsyncMock(side_effect=UpdateHeld('changed')))
    d=downloader(tmp_path,lambda req:response(status=500))
    try:
        r=await acquire_corpus(tmp_path/'state',options(sources=('helloao',)),store=store,downloader=d,adapters={'helloao':Source})
        assert r['counts']['held_update']==1 and r['status']=='failed'
    finally:await d.client.aclose()

async def test_cancellation_or_fatal_disk_error_leaves_receipt(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path)
    class Source(FixtureSource):candidates=[c];failures={c.key:DiskLimitError('fixture reserve')}
    d=downloader(tmp_path,lambda req:response(status=500))
    try:
        with pytest.raises(DiskLimitError):
            await acquire_corpus(tmp_path/'state',options(sources=('helloao',),download_only=True),downloader=d,adapters={'helloao':Source})
        r=json.loads((tmp_path/'state/reports/latest.json').read_text())
        assert r['status']=='failed' and 'DiskLimitError' in r['fatal_error']
    finally:await d.client.aclose()

async def test_discover_does_not_download_corpus(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path)
    class Source(FixtureSource):
        candidates=[c]
        async def prepare(self,*args):raise AssertionError('Discover must not fetch verse files')
    d=downloader(tmp_path,lambda req:response(status=500))
    try:
        r=await acquire_corpus(tmp_path/'state',options(sources=('helloao',)),
            discover_only=True,downloader=d,adapters={'helloao':Source})
        assert r['status']=='planned' and r['counts']['planned']==1 and not r['database_written']
    finally:await d.client.aclose()

async def test_validation_failure_refreshes_stale_cache_once(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path);called=[]
    class Source(FixtureSource):
        candidates=[c];path=p;payload=x;inventory=inv
        async def prepare(self,c,refresh):
            called.append(refresh)
            if not refresh:raise ValueError('stale data generation')
            return await super().prepare(c,refresh)
    d=downloader(tmp_path,lambda req:response(status=500))
    try:
        r=await acquire_corpus(tmp_path/'state',options(sources=('helloao',),download_only=True),downloader=d,adapters={'helloao':Source})
        assert r['status']=='succeeded' and called==[False,True]
        assert r['items'][0]['validation_refresh_retry']
    finally:await d.client.aclose()

def test_source_ids_and_language_filters_are_not_ambiguous(tmp_path):
    c,*_=hello_fixture(tmp_path)
    assert selected(c,options(editions=('helloao:sample',)))
    assert not selected(c,options(editions=('getbible:sample',)))
    assert selected(c,options(languages=('en',)))
    assert not selected(c,options(languages=('ru',)))

def test_concurrent_cache_job_is_refused(tmp_path):
    with process_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with process_lock(tmp_path):pass


def test_cache_lock_is_released_after_failure(tmp_path):
    with pytest.raises(ValueError):
        with process_lock(tmp_path):
            raise ValueError('synthetic failure')
    with process_lock(tmp_path):
        assert (tmp_path/'multisource.lock').exists()

@pytest.mark.parametrize('status,expected',[('succeeded',0),('planned',0),('skipped',0),('partial',2),('failed',1),('interrupted',1)])
def test_machine_readable_exit_codes(status,expected):assert exit_code({'status':status})==expected

async def test_cli_configuration_needs_no_database_or_token(tmp_path,monkeypatch):
    monkeypatch.delenv('BOT_TOKEN',raising=False);monkeypatch.delenv('DATABASE_URL',raising=False)
    r=await run(parser().parse_args(['config','--sources','getbible,helloao','--profile','all-open','--cache-dir',str(tmp_path)]))
    assert r['status']=='planned' and r['options']['sources']==('getbible','helloao')
    assert not list(tmp_path.iterdir())

async def test_qualified_edition_lookup_uses_source_alias():
    c=SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    assert await bible.find_translation(c,'helloao:BSB') is None
    assert c.fetchrow.await_args.args[1:]==('helloao','BSB','HelloAO')

async def test_native_numbering_not_falsely_mapped_to_legacy_topics():
    c=SimpleNamespace(fetch=AsyncMock(return_value=[]))
    assert await bible.topic_verse(c,{'numbering_system':'native:KJV'}) is None
    c.fetch.assert_not_awaited()

async def test_one_bad_catalog_entry_does_not_hide_other_editions(tmp_path):
    candidate,_,_,_=get_fixture(tmp_path)
    d=downloader(tmp_path,lambda req:response({'sample':candidate.raw,'invalid':{'lang':'not a language','abbreviation':'invalid'}}))
    try:
        source=GetBible(d)
        choices=await source.catalog(True)
        assert len(choices)==1 and choices[0].metadata.translation_id=='sample'
        assert len(source.notes)==1 and 'invalid' in source.notes[0]
    finally:await d.client.aclose()
