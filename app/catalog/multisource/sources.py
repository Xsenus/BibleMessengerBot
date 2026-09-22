"""Three real acquisition adapters, each with separate identifiers and attribution."""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.catalog.audit import audit_corpus
from app.catalog.models import DownloadedTranslation, LicenseInfo, TranslationMeta
from app.catalog.multisource.licenses import resolve_hello_license
from app.catalog.multisource.parsers import fingerprint, parse_getbible, parse_helloao, verify_records
from app.catalog.multisource.transport import Downloader, HttpStatusError, digest_file
from app.catalog.multisource.types import Candidate, Prepared, safe_id
from app.catalog.profiles import ISO_639_1_TO_3
from app.catalog.references import pair_verses, parse_reference_lines
from app.catalog.source import BibleNlpSource, SOURCE_REVISION, _to_date


# Source language codes remain distinct: modern/biblical Hebrew and Arabic dialects are not one language.
ISO_SOURCE = {**ISO_639_1_TO_3, 'he':'heb','iw':'heb','ar':'ara','fa':'fas','zh':'zho','ms':'msa','sw':'swa',
              'sq':'sqi','be':'bel','et':'est','lt':'lit','lv':'lav','is':'isl','ka':'kat','hy':'hye','eo':'epo','mt':'mlt','cy':'cym','eu':'eus','ca':'cat','gl':'glg','nb':'nob','nn':'nno','bo':'bod','my':'mya'}
LANGUAGE_FAMILIES = ({'ara','arb'}, {'fas','pes'}, {'zho','cmn'}, {'msa','zlm'}, {'swa','swh'}, {'heb','hbo'})


def source_language(code: str) -> str:
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*',code):
        raise ValueError('Invalid source language code')
    primary=code.replace('_','-').split('-')[0].lower()
    return ISO_SOURCE.get(primary,primary)


def language_filter_codes(code: str) -> set[str]:
    original=source_language(code)
    for family in LANGUAGE_FAMILIES:
        if original in family:return set(family)
    return {original}


def make_meta(identifier: str,language: str,title: str,*,name: str='',english_name: str='',license_type: str='',
              license_url: str='',notice: str='',url: str='',direction: str='ltr',short_title: str='',extra=None) -> TranslationMeta:
    return TranslationMeta(source_language(language),safe_id(identifier),name or language,english_name or name or language,
        str(title),str(title),True,notice,url,0,0,0,0,0,0,0,0,0,
        direction.lower() if direction.lower() in {'ltr','rtl'} else 'ltr',True,short_title,'',None,
        LicenseInfo(identifier,license_type,license_url=license_url),extra or {})


class Source:
    slug: str
    def __init__(self,downloader: Downloader):
        self.downloader=downloader
        self.notes: list[str]=[]
    async def catalog(self,refresh: bool) -> list[Candidate]:
        raise NotImplementedError
    async def permission(self,candidate: Candidate,refresh: bool) -> Candidate:
        return candidate
    async def prepare(self,candidate: Candidate,refresh: bool) -> Prepared:
        raise NotImplementedError


class BibleNlp(Source):
    slug='biblenlp'
    def __init__(self,downloader: Downloader):
        super().__init__(downloader);self.revision=SOURCE_REVISION;self.refs=None

    async def catalog(self,refresh: bool) -> list[Candidate]:
        requested=os.getenv('BIBLENLP_REVISION','latest').strip()
        if requested=='latest':
            try:
                repo,_=await self.downloader.json('https://api.github.com/repos/BibleNLP/ebible',refresh=refresh,max_bytes=1024**2)
                branch=safe_id(repo['default_branch'])
                data,_=await self.downloader.json(f'https://api.github.com/repos/BibleNLP/ebible/commits/{branch}',refresh=refresh,max_bytes=8*1024**2)
                self.revision=data['sha']
            except Exception as exc:
                self.notes.append(f'Latest revision unavailable ({type(exc).__name__}); using pinned {SOURCE_REVISION}')
        else:self.revision=requested
        if not re.fullmatch(r'[0-9a-f]{40}',self.revision):raise ValueError('BIBLENLP_REVISION must be latest or a full 40-character commit SHA')
        self.base=f'https://raw.githubusercontent.com/BibleNLP/ebible/{self.revision}'
        translations=await self.downloader.fetch(self.base+'/metadata/translations.csv',refresh=refresh,max_bytes=8*1024**2)
        licenses=await self.downloader.fetch(self.base+'/metadata/licences.tsv',refresh=refresh,max_bytes=8*1024**2)
        parsed=BibleNlpSource._parse_translations(translations.read_text(encoding='utf-8-sig'),
            BibleNlpSource._parse_licenses(licenses.read_text(encoding='utf-8-sig')))
        if not parsed or len({m.translation_id for m in parsed})!=len(parsed):raise ValueError('Invalid BibleNLP catalog')
        return [Candidate(self.slug,m,f'{self.base}/corpus/{m.language_code}-{m.translation_id}.txt',self.revision,
                'BibleNLP Original versification',evidence={'catalog_sha256':digest_file(translations),'license_table_sha256':digest_file(licenses)}) for m in parsed]

    async def prepare(self,candidate: Candidate,refresh: bool) -> Prepared:
        if self.refs is None:
            try:path=await self.downloader.fetch(self.base+'/metadata/vref.txt',refresh=refresh,max_bytes=4*1024**2)
            except HttpStatusError as exc:
                if exc.status!=404:raise
                path=await self.downloader.fetch(self.base+'/vref.txt',refresh=refresh,max_bytes=4*1024**2)
            self.refs=parse_reference_lines(path.read_text(encoding='utf-8-sig').splitlines())
            self.refs_sha=digest_file(path)
        failure=None
        for relative in BibleNlpSource.corpus_candidates(candidate.metadata):
            url=f'{self.base}/{relative}'
            try:
                path=await self.downloader.fetch(url,refresh=refresh,max_bytes=64*1024**2)
                candidate=replace(candidate,url=url);break
            except HttpStatusError as exc:
                if exc.status!=404:raise
                failure=exc
        else:
            raise FileNotFoundError(f'No corpus file for {candidate.key}') from failure
        records=verify_records(pair_verses(self.refs,path.read_text(encoding='utf-8-sig').splitlines()))
        audit=audit_corpus(candidate.metadata,self.refs,records)
        # Coarse structural health still does not certify a printed edition.
        if candidate.metadata.total_verses and len(records)<candidate.metadata.total_verses*0.80:
            raise ValueError('BibleNLP text is below 80% of advertised reference coverage; quarantined')
        return Prepared(candidate,DownloadedTranslation(candidate.metadata,path,candidate.url,digest_file(path)),
            self.refs,records,audit,fingerprint(records,candidate.numbering),diagnostics={'reference_file_sha256':self.refs_sha})


class GetBible(Source):
    slug='getbible'
    base='https://api.getbible.net/v2'

    async def catalog(self,refresh: bool) -> list[Candidate]:
        data,path=await self.downloader.json(self.base+'/translations.json',refresh=refresh,max_bytes=16*1024**2)
        if not isinstance(data,dict) or not data:raise ValueError('Invalid getBible catalog')
        result=[]
        for key,row in data.items():
            try:
                if not isinstance(row,dict):raise ValueError('Invalid getBible catalog row')
                identifier=safe_id(row.get('abbreviation',key))
                if identifier!=key:raise ValueError('getBible catalog key mismatch')
                meta=make_meta(identifier,row.get('lang',''),row.get('translation',identifier),
                    name=row.get('language',''),license_type=row.get('distribution_license',''),
                    notice=row.get('distribution_license',''),url=row.get('distribution_source',''),direction=row.get('direction','ltr'),
                    short_title=identifier,extra={'raw_language':row.get('lang'), 'source_catalog':row})
                revision=str(row.get('sha') or row.get('distribution_version_date') or digest_file(path))
                result.append(Candidate(self.slug,meta,f'{self.base}/{identifier}.json',revision,
                    'native:'+str(row.get('distribution_versification') or 'getBible-unspecified'),raw=row,
                    evidence={'catalog_sha256':digest_file(path),'upstream_sha_kind':'source-provided semantic SHA; not assumed to be file SHA-256'}))
            except (ValueError,KeyError,TypeError,AttributeError) as exc:
                self.notes.append(f'Catalog entry {str(key)[:100]} rejected: {type(exc).__name__}: {str(exc)[:200]}')
        if not result:raise ValueError('getBible has no structurally usable catalog entries')
        return result

    async def prepare(self,candidate: Candidate,refresh: bool) -> Prepared:
        payload,path=await self.downloader.json(candidate.url,refresh=refresh)
        books,_=await self.downloader.json(f'{self.base}/{candidate.metadata.translation_id}/books.json',refresh=refresh,max_bytes=2*1024**2)
        return parse_getbible(candidate,path,payload,books)


class HelloAO(Source):
    slug='helloao'
    base='https://bible.helloao.org/api'

    async def catalog(self,refresh: bool) -> list[Candidate]:
        data,path=await self.downloader.json(self.base+'/available_translations.json',refresh=refresh,max_bytes=16*1024**2)
        rows=data.get('translations')
        if not isinstance(rows,list) or not rows:raise ValueError('Invalid HelloAO catalog')
        identifiers=[row.get('id') for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str)]
        if len(identifiers)!=len(set(identifiers)):raise ValueError('Ambiguous duplicate HelloAO IDs')
        result=[];seen=set()
        for row in rows:
            try:
                identifier=safe_id(row['id'])
                if identifier in seen:raise ValueError('Duplicate HelloAO ID')
                seen.add(identifier)
                meta=make_meta(identifier,row['language'],row['name'],name=row.get('languageName') or row['language'],
                    english_name=row.get('languageEnglishName') or row['language'],url=row.get('website',''),direction=row.get('textDirection','ltr'),
                    short_title=row.get('shortName',identifier),extra={'source_catalog':row})
                result.append(Candidate(self.slug,meta,f'{self.base}/{identifier}/complete.simple.json',
                    str(row.get('sha256') or digest_file(path)),'native:HelloAO',raw=row,evidence={'catalog_sha256':digest_file(path)}))
            except (ValueError,KeyError,TypeError,AttributeError) as exc:
                self.notes.append(f'HelloAO catalog entry rejected: {type(exc).__name__}: {str(exc)[:200]}')
        if not result:raise ValueError('HelloAO has no structurally usable catalog entries')
        return result

    async def permission(self,candidate: Candidate,refresh: bool) -> Candidate:
        return await resolve_hello_license(candidate,self.downloader,refresh)

    async def prepare(self,candidate: Candidate,refresh: bool) -> Prepared:
        # Never concatenate thousands of independently updated chapters into a purported whole edition.
        # If the complete endpoint is absent, record a failure and let other sources supply editions.
        payload,path=await self.downloader.json(candidate.url,refresh=refresh)
        books,_=await self.downloader.json(f'{self.base}/{candidate.metadata.translation_id}/books.json',refresh=refresh,max_bytes=2*1024**2)
        return parse_helloao(candidate,path,payload,books)


ADAPTERS={'biblenlp':BibleNlp,'getbible':GetBible,'helloao':HelloAO}
