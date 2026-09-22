"""Real HTTP-client logic exercised with controlled byte streams; no live network claims."""
from __future__ import annotations
import hashlib
import json
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from email.utils import format_datetime
import httpx
import pytest
from app.catalog.multisource.transport import Downloader,DownloadError,DiskLimitError,HttpStatusError,validate_url,retry_seconds,write_json
from tests.multisource_fixtures import Bytes,response,downloader

URL='https://bible.helloao.org/api/fixture/complete.simple.json'

@pytest.fixture(autouse=True)
def no_network_wait(monkeypatch):
    monkeypatch.setattr('app.catalog.multisource.transport.retry_seconds',lambda *args:0)

@pytest.mark.parametrize('url',[
    'http://bible.helloao.org/api/x','https://evil.example/x','https://127.0.0.1/x',
    'https://user:pass@bible.helloao.org/x','https://bible.helloao.org:8080/x',
    'https://raw.githubusercontent.com/attacker/repo/main/x',
    'https://api.github.com/users/me','https://api.github.com/repos/Other/Repo',
    'https://bible.helloao.org/api/abc\nInjected',
])
def test_sources_cannot_supply_arbitrary_urls(url):
    with pytest.raises(ValueError):validate_url(url)

def test_github_metadata_root_is_allowed():
    assert validate_url('https://api.github.com/repos/BibleNLP/ebible')

def test_retry_after_numeric_and_date_bounds():
    # Directly imported function is deliberately not monkeypatched.
    assert retry_seconds('0',1)==0
    assert retry_seconds('999',1)==120
    assert 0<=retry_seconds(format_datetime(datetime.now(timezone.utc)+timedelta(seconds=20)),1)<=20
    assert retry_seconds('nonsense',1)==2

async def test_cache_hash_and_corruption_redownload(tmp_path):
    calls=[]
    def handler(request):calls.append(request);return response(content=b'valid content')
    d=downloader(tmp_path,handler)
    try:
        path=await d.fetch(URL)
        assert path.read_bytes()==b'valid content'
        assert await d.fetch(URL)==path and len(calls)==1 and d.cache_hits==1
        path.write_bytes(b'broken')
        assert (await d.fetch(URL)).read_bytes()==b'valid content' and len(calls)==2
        info=json.loads(d.paths(URL)[1].read_text())
        assert info['sha256']==hashlib.sha256(b'valid content').hexdigest()
    finally:await d.client.aclose()

async def test_strong_etag_range_resume(tmp_path):
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)==1:
            return response(headers={'Content-Length':'10','ETag':'"one"'},
                stream=Bytes(b'ABCDE',error=httpx.ReadError('broken pipe')))
        assert request.headers['Range']=='bytes=5-'
        assert request.headers['If-Range']=='"one"'
        return response(status=206,content=b'FGHIJ',headers={'Content-Range':'bytes 5-9/10','Content-Length':'5','ETag':'"one"'})
    d=downloader(tmp_path,handler)
    try:
        assert (await d.fetch(URL)).read_bytes()==b'ABCDEFGHIJ'
        assert d.resumes==1 and len(requests)==2
    finally:await d.client.aclose()

async def test_resume_survives_a_new_downloader_process(tmp_path):
    def broken(request):return response(headers={'Content-Length':'10','ETag':'"one"'},stream=Bytes(b'ABCDE',error=httpx.ReadError('stop')))
    d=downloader(tmp_path,broken);d.options=replace(d.options,attempts=1)
    try:
        with pytest.raises(DownloadError):await d.fetch(URL)
    finally:await d.client.aclose()
    def resumed(request):
        assert request.headers['Range']=='bytes=5-'
        return response(status=206,content=b'FGHIJ',headers={'Content-Range':'bytes 5-9/10','ETag':'"one"'})
    d=downloader(tmp_path,resumed)
    try:assert (await d.fetch(URL)).read_bytes()==b'ABCDEFGHIJ'
    finally:await d.client.aclose()

async def test_ignored_range_restarts_without_appending(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:return response(headers={'Content-Length':'10','ETag':'"old"'},stream=Bytes(b'OLD',error=httpx.ReadError('break')))
        assert request.headers['Range']=='bytes=3-'
        return response(content=b'NEW CONTENT',headers={'ETag':'"new"'})
    d=downloader(tmp_path,handler)
    try:assert (await d.fetch(URL)).read_bytes()==b'NEW CONTENT'
    finally:await d.client.aclose()

@pytest.mark.parametrize('range_header',['bytes 0-9/10','bytes 5-8/10','nonsense','bytes 5-9/*'])
async def test_invalid_range_is_not_committed(tmp_path,range_header):
    d=downloader(tmp_path,lambda req:response(status=206,content=b'FGHIJ',headers={'Content-Range':range_header,'ETag':'"one"'}))
    target,info,part,part_info=d.paths(URL)
    part.write_bytes(b'ABCDE');write_json(part_info,{'url':URL,'validator':'"one"','etag':'"one"'})
    try:
        with pytest.raises(DownloadError):await d.fetch(URL)
        assert not target.exists() and not part.exists()
    finally:await d.client.aclose()

async def test_changed_etag_invalidates_partial(tmp_path):
    d=downloader(tmp_path,lambda req:response(status=206,content=b'FGHIJ',headers={'Content-Range':'bytes 5-9/10','ETag':'"two"'}))
    target,info,part,part_info=d.paths(URL);part.write_bytes(b'ABCDE')
    write_json(part_info,{'url':URL,'validator':'"one"','etag':'"one"'})
    try:
        with pytest.raises(DownloadError):await d.fetch(URL)
        assert not target.exists() and not part.exists()
    finally:await d.client.aclose()

async def test_retry_429_then_success(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        return response(status=429,content=b'',headers={'Retry-After':'0'}) if len(calls)==1 else response(content=b'OK')
    d=downloader(tmp_path,handler)
    try:assert (await d.fetch(URL)).read_bytes()==b'OK' and len(calls)==2
    finally:await d.client.aclose()

async def test_404_not_retried(tmp_path):
    d=downloader(tmp_path,lambda req:response(status=404,content=b'no'))
    try:
        with pytest.raises(HttpStatusError) as e:await d.fetch(URL)
        assert e.value.status==404 and d.requests==1
    finally:await d.client.aclose()

async def test_redirect_ssrf_blocked(tmp_path):
    d=downloader(tmp_path,lambda req:response(status=302,content=b'',headers={'Location':'http://169.254.169.254/latest'}))
    try:
        with pytest.raises(ValueError):await d.fetch(URL)
        assert d.requests==1
    finally:await d.client.aclose()

@pytest.mark.parametrize('kind',['declared','stream','free_disk','cache'])
async def test_disk_and_length_limits(tmp_path,kind,monkeypatch):
    headers={'Content-Length':'2000'} if kind=='declared' else {}
    d=downloader(tmp_path,lambda req:response(content=b'x'*2000,headers=headers))
    if kind=='free_disk':d.options=replace(d.options,min_free_bytes=10**18)
    if kind=='cache':d.options=replace(d.options,max_cache_bytes=100)
    try:
        with pytest.raises((DownloadError,DiskLimitError)):await d.fetch(URL,max_bytes=1000 if kind in {'declared','stream'} else 3000)
        assert not d.paths(URL)[0].exists()
    finally:await d.client.aclose()

async def test_invalid_json_and_encoding_not_replaced(tmp_path):
    d=downloader(tmp_path,lambda req:response(content=b'{"text":"\xff"}'))
    try:
        with pytest.raises(UnicodeError):await d.json(URL)
    finally:await d.client.aclose()

async def test_gzip_ignoring_identity_refused(tmp_path):
    d=downloader(tmp_path,lambda req:response(content=b'fakecompressed',headers={'Content-Encoding':'gzip'}))
    try:
        with pytest.raises(DownloadError):await d.fetch(URL)
    finally:await d.client.aclose()

@pytest.mark.parametrize('raw',[b'{"same":1,"same":2}',b'{"n":NaN}',b'{"n":Infinity}'])
async def test_nonstandard_or_duplicate_json_refused(tmp_path,raw):
    d=downloader(tmp_path,lambda req:response(content=raw))
    try:
        with pytest.raises(ValueError):await d.json(URL)
    finally:await d.client.aclose()
