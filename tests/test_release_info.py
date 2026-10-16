"""Изолированная проверка безопасных метаданных и отказов GitHub; настоящая сеть и production не используются."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from app.services import release_info as info
from app.web import api_release

SHA = 'a' * 40
OLD = 'b' * 40
STAMP = '2026-10-16T02:00:00+00:00'


def snapshot(sha=SHA, status='succeeded'):
    return {'commit':sha, 'previous_commit':OLD, 'status':status, 'stage':'completed',
            'started_at':STAMP, 'finished_at':STAMP, 'updated_at':STAMP,
            'services':[{'name':'bot','commit':sha,'health':'healthy'}, {'name':'maxbot','commit':sha,'health':'healthy'}]}


def files(monkeypatch, tmp_path, current=None, latest=None):
    (tmp_path / 'VERSION').write_text('1.20.0\n')
    build = tmp_path / 'build.json'
    build.write_text(json.dumps({'commit':SHA,'built_at':STAMP,'password':'never expose'}))
    status = tmp_path / 'status.json'
    if current or latest:
        status.write_text(json.dumps({'schema':1,'current':current,'last_attempt':latest or current}))
    monkeypatch.setattr(info, 'ROOT', tmp_path)
    monkeypatch.setattr(info, 'BUILD_FILE', build)
    monkeypatch.setattr(info, 'STATUS_FILE', status)
    return build, status


def run_record(**changes):
    return {'id':1234,'head_branch':'main','head_sha':SHA,'event':'workflow_run','status':'completed',
            'conclusion':'success','run_started_at':STAMP,'updated_at':STAMP,'run_attempt':2,
            'html_url':'https://malicious.invalid/?secret', 'secret':'never expose', **changes}


def test_build_is_from_image_and_does_not_follow_host_checkout(monkeypatch, tmp_path):
    build, status = files(monkeypatch, tmp_path, snapshot(OLD), snapshot(SHA, 'failed'))
    assert info.build_info()['commit'] == SHA
    server = info.server_info()
    assert server['current']['commit'] == OLD and server['last_attempt']['status'] == 'failed'
    assert 'never expose' not in json.dumps([info.build_info(), server])
    build.unlink()
    assert info.build_info()['commit'] is None


@pytest.mark.parametrize('payload', ['not json', '[' + '0,'*70000 + '0]', '{"schema":1,"current":{"commit":"a"}}'], ids=['invalid-json','oversized','invalid-record'])
def test_corrupt_and_oversized_snapshot_is_unknown(monkeypatch, tmp_path, payload):
    build, status = files(monkeypatch, tmp_path)
    status.write_text(payload)
    assert not info.server_info()['available']


def test_status_whitelists_fields_and_rejects_false_success(monkeypatch, tmp_path):
    data = snapshot()
    data.update(environment={'token':'never expose'}, log='never expose')
    data['services'].append({'name':'/etc/passwd','health':'healthy','commit':SHA})
    files(monkeypatch, tmp_path, data)
    result = info.server_info()
    assert len(result['current']['services']) == 2 and 'never expose' not in json.dumps(result)
    assert info.attempt({**data,'finished_at':None}) is None
    assert info.attempt({**data,'status':[]}) is None
    assert info.moment('2026-10-16T02:00:00') is None
    assert info.commit('a'*40+';id') is None


def test_old_running_attempt_is_not_presented_as_confirmed_active(monkeypatch, tmp_path):
    data = snapshot(status='running')
    data['finished_at'] = None
    data['updated_at'] = (datetime.now(UTC)-timedelta(minutes=91)).isoformat()
    files(monkeypatch,tmp_path,snapshot(OLD),data)
    assert info.server_info()['stale']


@pytest.mark.asyncio
async def test_github_has_one_request_for_concurrent_readers_and_safe_urls():
    calls = []
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200,json={'workflow_runs':[run_record()]})
    cache = info.GithubDeployments(httpx.MockTransport(handler))
    results = await asyncio.gather(*(cache.get() for _ in range(10)))
    assert len(calls) == 1 and all(result['available'] for result in results)
    assert results[0]['run']['url'] == info.REPOSITORY+'/actions/runs/1234'
    assert 'malicious' not in json.dumps(results) and 'never expose' not in json.dumps(results)
    assert 'authorization' not in calls[0].headers and calls[0].url.params['per_page']=='1'


@pytest.mark.asyncio
async def test_github_error_is_marked_stale_and_not_cached_as_current_success():
    responses = [httpx.Response(200,json={'workflow_runs':[run_record()]}), httpx.Response(403,text='secret error')]
    cache = info.GithubDeployments(httpx.MockTransport(lambda _:responses.pop(0)))
    first = await cache.get()
    cache.next_refresh = 0
    second = await cache.get()
    assert first['available'] and not second['available'] and second['stale']
    assert second['checked_at'] == first['checked_at'] and second['run'] == first['run']
    assert 'secret error' not in json.dumps(second)
    await cache.get()
    assert not responses


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    httpx.Response(429), httpx.Response(200,text='invalid'),
    httpx.Response(200,json={'workflow_runs':[run_record(head_sha='main')]}),
    httpx.Response(200,json={'workflow_runs':[run_record(head_branch='feature/untrusted')]}),
    httpx.Response(200,json={'workflow_runs':[run_record(id=True)]}),
])
async def test_bad_github_response_cannot_report_success(response):
    cache = info.GithubDeployments(httpx.MockTransport(lambda _:response))
    result = await cache.get()
    assert not result['available'] and result['run'] is None


@pytest.mark.asyncio
async def test_github_timeout_is_unknown_and_empty_list_is_valid():
    async def timeout(request):
        raise httpx.ReadTimeout('private network URL',request=request)
    result = await info.GithubDeployments(httpx.MockTransport(timeout)).get()
    assert not result['available'] and 'private' not in json.dumps(result)
    empty = await info.GithubDeployments(httpx.MockTransport(lambda _:httpx.Response(200,json={'workflow_runs':[]}))).get()
    assert empty['available'] and empty['run'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_db', [False, True])
async def test_release_endpoint_auth_and_independent_bot_checks(monkeypatch, tmp_path, fail_db):
    files(monkeypatch, tmp_path, snapshot())
    calls = []
    class Github:
        async def get(self):
            calls.append('github')
            return {'available':False,'run':None,'checked_at':None,'stale':False}
    class Connection:
        async def fetch(self, query):
            if fail_db:
                raise OSError('synthetic private connection string')
            return [{'service':'bot','last_seen':datetime.now(UTC),'fresh':True},
                    {'service':'maxbot','last_seen':datetime.now(UTC),'fresh':False}]
    @asynccontextmanager
    async def acquire():
        yield Connection()
    async def pool():
        return SimpleNamespace(acquire=acquire)
    def auth(x_admin_key: str | None = Header(None)):
        if x_admin_key != 'synthetic':
            raise HTTPException(401)
    monkeypatch.setattr(api_release, 'github', Github())
    app = FastAPI()
    app.include_router(api_release.build_router(auth,pool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/admin/release')).status_code == 401 and not calls
        response = await client.get('/api/admin/release',headers={'X-Admin-Key':'synthetic'})
    assert response.status_code == 200 and response.headers['Cache-Control']=='no-store'
    data = response.json()
    assert data['bots'][0]['verified'] and data['bots'][0]['live'] is (None if fail_db else True)
    assert data['bots'][1]['verified'] and data['bots'][1]['live'] is (None if fail_db else False)
    assert data['bots'][2]['expected'] is False
    assert not data['github']['available'] and data['build']['commit']==SHA
    assert 'private connection' not in response.text
