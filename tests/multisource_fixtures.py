"""Synthetic protocol fixtures. These are NOT biblical texts and never enter a production catalog."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path
import httpx
from app.catalog.multisource.types import Candidate, ImportOptions
from app.catalog.multisource.sources import make_meta
from app.catalog.multisource.transport import Downloader

class Bytes(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, error: Exception | None = None):
        self.chunks=chunks;self.error=error
    async def __aiter__(self):
        for chunk in self.chunks:yield chunk
        if self.error:raise self.error

def response(data=None, *, status=200, content=None, headers=None, stream=None):
    content=content if content is not None else json.dumps(data,ensure_ascii=False).encode()
    return httpx.Response(status,headers=headers or {},stream=stream or Bytes(content))

def options(**changes):
    return ImportOptions(min_free_bytes=0,max_cache_bytes=50*1024**2,max_file_bytes=10*1024**2,
        requests_per_second=10,attempts=2,**changes)

def downloader(tmp_path, handler, **changes):
    d=Downloader(tmp_path/'http',options(**changes),httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async def fast(url):pass
    d._rate=fast
    return d

def hello_fixture(tmp_path:Path, identifier='sample', text='SYNTHETIC NOT SCRIPTURE A&B <words>'):
    raw={'id':identifier,'language':'eng','name':'Synthetic fixture','numberOfBooks':1,
        'totalNumberOfChapters':1,'totalNumberOfVerses':2,'sha256':'revision-one'}
    inventory={'translation':raw,'books':[{'id':'GEN','name':'Fixture Genesis','numberOfChapters':1,
        'firstChapterNumber':1,'lastChapterNumber':1,'totalNumberOfVerses':2}]}
    payload={'translation':raw,'books':[{'id':'GEN','name':'Fixture Genesis','chapters':[{'numberOfVerses':2,
        'chapter':{'number':1,'content':[{'type':'heading','text':'Not a verse'},
            {'type':'verse','number':1,'text':text},{'type':'verse','number':2,'text':'SYNTHETIC SECOND'}]}}]}]}
    m=make_meta(identifier,'eng','Synthetic fixture',license_type='Public Domain',notice='Public Domain')
    c=Candidate('helloao',m,f'https://bible.helloao.org/api/{identifier}/complete.simple.json','revision-one','native:HelloAO',raw=raw)
    path=tmp_path/f'{identifier}.json';path.write_text(json.dumps(payload,ensure_ascii=False), encoding='utf-8')
    return c,path,payload,inventory

def get_fixture(tmp_path,identifier='sample'):
    raw={'abbreviation':identifier,'lang':'en','translation':'Synthetic fixture','distribution_license':'Public Domain',
         'distribution_versification':'KJV','sha':'semantic-sha'}
    m=make_meta(identifier,'en','Synthetic fixture',license_type='Public Domain',notice='Public Domain')
    c=Candidate('getbible',m,f'https://api.getbible.net/v2/{identifier}.json','semantic-sha','native:KJV',raw=raw)
    payload={'abbreviation':identifier,'lang':'en','sha':'semantic-sha','books':[
        {'nr':62,'name':'Fixture 1 John','chapters':[{'chapter':1,'verses':[
            {'chapter':1,'verse':1,'text':'SYNTHETIC A'},{'chapter':1,'verse':2,'text':'SYNTHETIC B'}]}]}]}
    inv={'62':{'book_nr':62,'name':'Fixture 1 John','chapters':1}}
    path=tmp_path/f'{identifier}.json';path.write_text(json.dumps(payload))
    return c,path,payload,inv
