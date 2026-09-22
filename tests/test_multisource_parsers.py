"""Offline format, coverage, permission and identifier invariants."""
from __future__ import annotations
import copy
from dataclasses import replace
import pytest
from app.catalog.models import LicenseInfo
from app.catalog.audit import OT,NT
from app.catalog.multisource.types import ImportOptions,safe_id
from app.catalog.multisource.sources import source_language,language_filter_codes,make_meta
from app.catalog.multisource.licenses import parse_license_html,ebible_id
from app.catalog.multisource.parsers import (parse_helloao,parse_getbible,fingerprint,positive,book_code,
    text_value,verify_records,append_verse,native_audit,CHAPTER_FLOOR)
from app.catalog.policy import decide_license
from tests.multisource_fixtures import hello_fixture,get_fixture

@pytest.mark.parametrize('value',['../../x','https://example.com','with space','x?token=y','',None,'a'*101])
def test_unsafe_id(value):
    with pytest.raises(ValueError):safe_id(value)

@pytest.mark.parametrize('code,expected',[('en','eng'),('he','heb'),('hbo','hbo'),('ar','ara'),('arb','arb'),
    ('zh-Hant','zho'),('pt-BR','por'),('ru','rus'),('zh-Hans','zho')])
def test_language_identity(code,expected):assert source_language(code)==expected

def test_families_are_filters_not_rewrites():
    assert language_filter_codes('ar')=={'ara','arb'}
    assert source_language('ar')!=source_language('arb')

@pytest.mark.parametrize('value',[0,-1,True,None,'1a',1.5,'10001'])
def test_invalid_coordinates(value):
    with pytest.raises(ValueError):positive(value,'test')

@pytest.mark.parametrize('changes',[{'sources':()}, {'sources':('biblenlp','biblenlp')},
    {'sources':('unknown',)}, {'profile':'all'}, {'max_editions':0}, {'attempts':99},
    {'requests_per_second':float('nan')},{'requests_per_second':float('inf')},
    {'languages':('en/../x',)}, {'editions':('free-id',)}, {'editions':('helloao:../id',)}])
def test_bad_options(changes):
    with pytest.raises(ValueError):ImportOptions(**changes)

def test_hello_shape_ranges_and_exact_unicode(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path,text='Текст 🙂\n第二行 العربية')
    result=parse_helloao(c,p,x,inv)
    assert result.records[0][3]=='Текст 🙂\n第二行 العربية'
    assert result.audit.verses==2 and result.audit.books==1
    assert not result.audit.canonical_66_complete
    assert result.book_names['GEN']=='Fixture Genesis'
    assert result.diagnostics['skipped_annotation_objects']==1
    assert result.candidate.metadata.ot_verses==2

def test_hello_joined_verses_not_duplicated(tmp_path):
    c,p,x,inv=hello_fixture(tmp_path)
    ch=x['books'][0]['chapters'][0]['chapter']
    ch['content']=[{'type':'verse','number':'1-2','text':'SYNTHETIC JOINED'}]
    r=parse_helloao(c,p,x,inv)
    assert len(r.records)==2 and r.records[1][4] is True and r.records[1][3]==''
    assert r.audit.visible_verses==1

@pytest.mark.parametrize('mutation',[
    lambda x,i:x['translation'].update(id='wrong'),
    lambda x,i:x['translation'].update(language='rus'),
    lambda x,i:x['books'].append(copy.deepcopy(x['books'][0])),
    lambda x,i:x['books'][0]['chapters'].append(copy.deepcopy(x['books'][0]['chapters'][0])),
    lambda x,i:x['books'][0]['chapters'][0]['chapter'].update(number=2),
    lambda x,i:i['books'].append({'id':'EXO','numberOfChapters':1}),
    lambda x,i:i['books'][0].update(totalNumberOfVerses=99),
    lambda x,i:x['books'][0]['chapters'][0]['chapter']['content'].append({'type':'unknown','text':'lost'}),
    lambda x,i:x['books'][0]['chapters'][0]['chapter']['content'][1].update(text=None),
    lambda x,i:x['books'][0]['chapters'][0]['chapter']['content'][1].update(text='x\x00'),
    lambda x,i:x['books'][0]['chapters'][0]['chapter']['content'][2].update(number=1),
])
def test_hello_rejects_inconsistent_edition_atomically(tmp_path,mutation):
    c,p,x,inv=hello_fixture(tmp_path)
    # Avoid mutating the independent candidate/catalog dict in a fixture.
    x=copy.deepcopy(x);inv=copy.deepcopy(inv)
    mutation(x,inv)
    with pytest.raises(ValueError):parse_helloao(c,p,x,inv)

def test_getbible_whole_translation_shape(tmp_path):
    r=parse_getbible(*get_fixture(tmp_path))
    assert r.records[0][:3]==('1JN',1,1)
    assert r.audit.coverage=='partial'
    assert r.book_names['1JN']=='Fixture 1 John'

@pytest.mark.parametrize('mutation',[
    lambda x,i:x.update(abbreviation='wrong'),lambda x,i:x.update(lang='ru'),
    lambda x,i:x.update(sha='changed'),lambda x,i:x['books'][0].update(nr=67),
    lambda x,i:x['books'][0]['chapters'][0]['verses'][0].update(chapter=2),
    lambda x,i:i.update({'1':{'nr':1}}),
    lambda x,i:x['books'][0]['chapters'][0]['verses'][1].update(verse=1),
    lambda x,i:i['62'].update(chapters=2),
])
def test_getbible_invalid_structure(tmp_path,mutation):
    c,p,x,inv=get_fixture(tmp_path);mutation(x,inv)
    with pytest.raises(ValueError):parse_getbible(c,p,x,inv)

def test_fingerprints_respect_numbering_and_text():
    rows=[('GEN',1,1,'SYNTHETIC',False,1)]
    assert fingerprint(rows,'A')!=fingerprint(rows,'B')
    assert fingerprint(rows,'A')==fingerprint([(*rows[0][:5],99)],'A')
    assert fingerprint(rows,'A')!=fingerprint([('GEN',1,1,'SYNTHETIC!',False,1)],'A')

def test_synthetic_66_names_not_full():
    rows=[(b,1,1,'SYNTHETIC',False,i+1) for i,b in enumerate(OT+NT)]
    meta=make_meta('fixture','en','Synthetic',license_type='Public Domain')
    a=native_audit(meta,rows,'native:test',{b:[1] for b in OT+NT},[])
    assert a.coverage=='partial' and not a.nt_complete and not a.canonical_66_complete

def test_full_structural_density_floor_is_not_printed_edition_proof():
    rows=[];chapters={b:list(range(1,CHAPTER_FLOOR[b]+1)) for b in OT+NT}
    for b in OT+NT:
        for c in chapters[b]:
            for v in range(1,26):rows.append((b,c,v,'SYNTHETIC STRUCTURE TEST',False,len(rows)+1))
    a=native_audit(make_meta('fixture','en','Synthetic'),rows,'native:test',chapters,[])
    assert a.canonical_66_complete and a.nt_complete
    assert 'NOT word-for-word' in a.verification_scope

@pytest.mark.parametrize('html,allowed',[
    ('<p>Public Domain</p>',True),
    ('<p>Not public domain</p>',False),
    ('<a href="https://creativecommons.org/licenses/by/4.0/">License</a>',True),
    ('<p>MIT software. Public domain might apply to some files.</p>',False),
    ('<p>Public Domain</p><p>All rights reserved</p>',False),
    ('<a href="https://creativecommons.org/licenses/by-nc-nd/4.0/">License</a>',False),
    ('<p>Public Domain</p><a href="https://creativecommons.org/licenses/by/4.0/">License</a>',False),
    ('<script>Public Domain</script><p>No license</p>',False),
    ('<a href="https://evil.example/creativecommons.org/licenses/by/4.0">License</a>',False),
])
def test_license_evidence_is_specific_and_fail_closed(html,allowed):
    kind,url,notice=parse_license_html(html)
    meta=make_meta('fixture','en','Fixture',license_type=kind,license_url=url,notice=notice)
    assert decide_license(meta).allowed is allowed

def test_only_rights_related_page_text_is_published():
    kind,url,notice=parse_license_html('<h1>Navigation '+('menu '*4000)+'</h1><p>Public Domain</p>')
    assert notice=='Public Domain'

def test_exact_ebible_identifier_only():
    assert ebible_id({'licenseUrl':'https://ebible.org/Scriptures/details.php?id=russyn'})=='russyn'
    assert ebible_id({'licenseUrl':'https://evil.example/?id=russyn'}) is None
