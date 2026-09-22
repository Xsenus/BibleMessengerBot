"""Regression checks for the concrete defects found in the supplied 1.1.0 archive."""
from __future__ import annotations
import ast
import asyncio
from dataclasses import replace
from datetime import datetime,time,timezone
import hashlib
import json
from pathlib import Path
import re
import string
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.bot.commands import parse_command,encode_callback,decode_callback,mode_name
from app.catalog.audit import audit_corpus,CORE,OT,NT
from app.catalog.models import TranslationMeta,LicenseInfo,VerseReference,DownloadedTranslation
from app.catalog.policy import decide_license
from app.catalog.references import parse_reference_lines,pair_verses,with_ordinals
from app.catalog.source import BibleNlpSource,SOURCE_REVISION
from app.config import Settings
from app.logging import redact
from app.services import bible
from app.services.formatting import escape,split_message,plain_text,utf16_length
from app.services.i18n import available_ui,catalogs,tr,ui_for_language
from app.services.outbox import Envelope,dispatch_chunk
from app.services.errors import SendError,UserError
from app.services.plans import plan_window
from app.services.rate_limit import intervals
from app.services.scheduling import parse_hhmm,next_occurrence

ROOT = Path(__file__).resolve().parents[1]


def metadata(**updates):
    """Synthetic metadata: never exported or represented as an actual Bible edition."""
    original = TranslationMeta(language_code='eng',translation_id='fixture',language_name='English',
        language_name_english='English',title='SYNTHETIC TEST — NOT SCRIPTURE',description='',
        redistributable=True,copyright_notice='Public Domain',publication_url='',ot_books=39,ot_chapters=39,
        ot_verses=39,nt_books=27,nt_chapters=27,nt_verses=27,dc_books=0,dc_chapters=0,dc_verses=0,
        text_direction='ltr',downloadable=True,short_title='Fixture',script='Latn',source_date=None,
        license=LicenseInfo('fixture','Public Domain'))
    return replace(original,**updates)


@pytest.mark.parametrize('locale',sorted(available_ui()))
def test_every_catalog_complete_and_splittable(locale):
    data = catalogs()[locale]
    assert set(data)==set(catalogs()['en'])
    fields = lambda s: sorted(field for _,field,_,_ in string.Formatter().parse(s) if field is not None)
    for key,value in data.items():
        assert fields(value)==fields(catalogs()['en'][key])
    text = '<blockquote>'+escape((' '.join(v for k,v in data.items() if not k.startswith('_'))+' 😀 ') * 5)+'</blockquote>'
    parts = split_message(text,250)
    assert len(parts)>1
    assert all(utf16_length(plain_text(p))<=250 for p in parts)
    assert re.sub(r'\s','',plain_text(text))==re.sub(r'\s','',''.join(plain_text(p) for p in parts))


@pytest.mark.parametrize('text',[
    '<b>'+'word &amp; 😀 '*500+'</b>',
    '<b>A <i>'+'я文 ع &lt;quoted&gt; '*500+'</i> Z</b>',
    '<a href="https://example.invalid/?a=1&amp;b=2">'+'a'*900+'</a>',
    '<blockquote>'+'एक पाठ '*400+'</blockquote>',
    '😀'*600,'x'*1200,'a '+'  \n '*300+'b',
])
def test_html_splits_without_losing_text(text):
    chunks=split_message(text,100)
    assert chunks and all(0<utf16_length(plain_text(c))<=100 for c in chunks)
    assert re.sub(r'\s','',plain_text(text))==re.sub(r'\s','',''.join(map(plain_text,chunks)))


@pytest.mark.parametrize('text',['<script>x</script>','<b>x</i>','<b>unfinished','<!--x-->','<b onclick="x">x</b>','<a href="javascript:1">x</a>'])
def test_reject_malformed_or_unsafe_html(text):
    with pytest.raises(ValueError):
        split_message(text)


@pytest.mark.parametrize('value',['9:00','24:00','09:0','10:30:00','1030','10:30+03:00','-1:00','00:60','00:00Z',' 09:00'])
def test_strict_hhmm(value):
    with pytest.raises(ValueError):
        parse_hhmm(value)


@pytest.mark.parametrize('days',[[],[0],[8],[True],['1']])
def test_invalid_weekdays(days):
    with pytest.raises(ValueError):
        next_occurrence(time(9),'UTC',days)


def test_dst_spring_gap_moves_forward():
    now = datetime(2026,3,29,0,0,tzinfo=timezone.utc)
    assert next_occurrence(time(2,30),'Europe/Amsterdam',now=now)==datetime(2026,3,29,1,30,tzinfo=timezone.utc)


def test_dst_fall_fold_never_sends_a_second_time_same_date():
    now = datetime(2026,10,25,0,45,tzinfo=timezone.utc)
    assert next_occurrence(time(2,30),'Europe/Amsterdam',now=now)==datetime(2026,10,26,1,30,tzinfo=timezone.utc)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        next_occurrence(time(9),'UTC',now=datetime(2026,1,1))


@pytest.mark.parametrize('total,days',[(1189,90),(1189,180),(1189,365),(260,90),(150,150),(151,150),(1400,365)])
def test_plan_partition_reads_every_chapter_exactly_once(total,days):
    covered=[]
    sizes=[]
    for day in range(days):
        first,last=plan_window(total,days,day)
        covered.extend(range(first,last));sizes.append(last-first)
    assert covered==list(range(total))
    assert max(sizes)-min(sizes)<=1
    assert plan_window(total,days,days)==(total,total)


@pytest.mark.parametrize('total,days,day',[(0,90,0),(20,90,0),(100,0,0),(100,90,-1)])
def test_invalid_plan_data(total,days,day):
    with pytest.raises(ValueError):
        plan_window(total,days,day)


@pytest.mark.parametrize('code,locale',[('rus','ru'),('en-US','en'),('ar','ar'),('arb','ar'),('hbo','he'),('cmn','zh'),('zh_Hant','zh'),('yue','zh'),('nonsense',None),('ces',None)])
def test_language_selection_never_implies_untranslated_locale(code,locale):
    assert ui_for_language(code)==locale


@pytest.mark.parametrize('typ,allowed',[('Public Domain',True),('CC0 1.0',True),('by',True),('CC BY-SA 4.0',True),
    ('by-nc',False),('by-nd',False),('by-nc-nd',False),('unknown',False),('Translated by Example; all rights reserved',False)])
def test_fail_closed_licensing(typ,allowed):
    assert decide_license(metadata(copyright_notice='',license=LicenseInfo('fixture',typ))).allowed==allowed


def test_licensing_prose_by_is_not_permission():
    assert not decide_license(metadata(license=None,copyright_notice='Copyright by a publisher. All rights reserved.')).allowed


def test_conflicting_license_fields_rejected():
    assert not decide_license(metadata(license=LicenseInfo('fixture','by',license_url='https://creativecommons.org/licenses/by-nc/4.0/'))).allowed


@pytest.mark.parametrize('field',['redistributable','downloadable'])
def test_source_distribution_flags_enforced(field):
    assert not decide_license(metadata(**{field:False})).allowed


def test_ranges_keep_anchor_and_endpoint():
    refs=parse_reference_lines('GEN 1:1\nGEN 1:2\nGEN 1:3\nGEN 1:4'.splitlines())
    pairs=pair_verses(refs,['fixture range','<range>','<range>','fixture next'])
    rows=with_ordinals(pairs)
    assert rows[0][6:]==(3,1)
    assert rows[1][7] is None and rows[2][7] is None
    assert rows[3][6:]==(4,2)


@pytest.mark.parametrize('lines',[
    ['<range>','text'],['','<range>'],['text\x00bad','x'],['text�','x']])
def test_corrupt_or_unanchored_range_rejected(lines):
    refs=parse_reference_lines(['GEN 1:1','GEN 1:2'])
    with pytest.raises(ValueError):
        pair_verses(refs,lines)


def test_cross_chapter_range_rejected():
    refs=parse_reference_lines(['GEN 1:1','GEN 2:1'])
    with pytest.raises(ValueError):
        pair_verses(refs,['fixture','<range>'])


@pytest.mark.parametrize('lines',[
    [],['GEN 1:1','GEN 1:1'],['GEN 2:1','GEN 1:1'],['GEN 0:1'],['GEN 1:0'],['GEN 1:1','EXO 1:1','GEN 1:2']])
def test_bad_reference_grid_rejected(lines):
    with pytest.raises(ValueError):
        parse_reference_lines(lines)


def fixture_corpus(codes):
    """One synthetic chapter/verse per book; tests structure, not real Bible completeness."""
    refs=[VerseReference(code,1,1,index+1) for index,code in enumerate(OT+NT)]
    pairs=pair_verses(refs,['SYNTHETIC TEXT' if ref.book_code in codes else '' for ref in refs])
    return refs,pairs


def test_actual_missing_book_overrides_claimed_full_coverage():
    refs,pairs=fixture_corpus(CORE-{'REV'})
    audit=audit_corpus(metadata(),refs,pairs)
    assert not audit.canonical_66_complete and audit.coverage=='partial'
    assert 'REV' in audit.missing_core_books
    assert any('claims full' in warning for warning in audit.warnings)


def test_new_testament_never_advertised_as_full_bible():
    refs,pairs=fixture_corpus(set(NT))
    audit=audit_corpus(metadata(),refs,pairs)
    assert audit.coverage=='nt' and audit.nt_complete and not audit.canonical_66_complete


def test_missing_chapter_overrides_present_books():
    refs,pairs=fixture_corpus(CORE)
    refs.insert(1,VerseReference('GEN',2,1,2))
    audit=audit_corpus(metadata(),refs,pairs)
    assert not audit.canonical_66_complete and 'GEN 2' in audit.missing_reference_chapters


def test_66_structural_flag_is_not_theological_completeness():
    refs,pairs=fixture_corpus(CORE)
    audit=audit_corpus(metadata(),refs,pairs)
    assert audit.canonical_66_complete and audit.books==66
    assert 'not independent proofreading' in audit.verification_scope


@pytest.mark.asyncio
async def test_download_stream_cache_reuse_and_tamper_detection(tmp_path):
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,content=b'test-source')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source=BibleNlpSource(tmp_path,client=client)
        path=await source.fetch_bytes_cached('https://fixture.invalid/data','data.txt')
        assert path.parent.name==SOURCE_REVISION
        assert path.read_bytes()==b'test-source'
        assert await source.fetch_bytes_cached('https://fixture.invalid/data','data.txt')==path
        assert len(calls)==1
        path.write_bytes(b'tampered')
        with pytest.raises(ValueError,match='checksum'):
            await source.fetch_bytes_cached('https://fixture.invalid/data','data.txt')


@pytest.mark.asyncio
async def test_download_size_limit_before_cache_commit(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'x'*1000))) as client:
        source=BibleNlpSource(tmp_path,client=client)
        with pytest.raises(ValueError,match='size'):
            await source.fetch_bytes_cached('https://fixture.invalid/data','too-big',max_bytes=100)
        assert not list(source.cache_dir.iterdir())


@pytest.mark.asyncio
async def test_exact_filename_selection_cannot_choose_another_edition(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(404))) as client:
        source=BibleNlpSource(tmp_path,client=client)
        source._corpus_index={'eng-fixture-extra.txt':'corpus/eng-fixture-extra.txt'}
        with pytest.raises(FileNotFoundError):
            await source.resolve_corpus_path(metadata())


@pytest.mark.parametrize('command,name,target',[
    ('/language en @channel','language','@channel'),('/settings -100123','settings','-100123'),
    ('/subscribe reading_plan 09:00 UTC bible-365 -1001','subscribe','-1001'),
    ('/search a @word','search',None),('/resolve 42 retry-duplicate-risk -10001','resolve','-10001')])
def test_explicit_destination_parser(command,name,target):
    parsed=parse_command(command)
    assert parsed.name==name and parsed.target==target


@pytest.mark.parametrize('text',['hello','/','/bad-command','/x@','/x'+'a'*4100])
def test_invalid_commands_rejected(text):
    with pytest.raises(UserError):
        parse_command(text)


@pytest.mark.parametrize('chat_id',[1,1000000001,-1001234567890])
def test_callback_roundtrip_binds_destination(chat_id):
    assert decode_callback(encode_callback('lang',chat_id,'rus'))==('lang',chat_id,'rus')


def test_callback_byte_limit_not_character_limit():
    with pytest.raises(ValueError):
        encode_callback('lang',-100,'文'*25)


@pytest.mark.parametrize('value',[0,-1,21,100,float('inf'),float('nan')])
def test_unsafe_global_rate_rejected(value):
    with pytest.raises(ValueError):
        intervals(1,value,1)


def test_groups_use_conservative_shared_interval():
    assert intervals(-100)[1]>=3
    assert intervals(123)[1]>=1


def test_secret_redaction_including_dsn(monkeypatch):
    monkeypatch.setenv('ADMIN_API_KEY','random-admin-private-value')
    text='123456789'+':'+'abcdefghijklmnopqrstuvwxyzABCDE postgresql://user:secret@host random-admin-private-value'
    redacted=redact(text)
    assert 'abcdefghijklmnopqrstuvwxyz' not in redacted and 'secret@' not in redacted
    assert 'random-admin-private-value' not in redacted


@pytest.mark.parametrize('env,value',[
    ('MAX_MESSAGE_LENGTH','4097'),('TELEGRAM_GLOBAL_RATE_PER_SECOND','30'),
    ('TELEGRAM_CHAT_RATE_PER_SECOND','2'),('DEFAULT_SEND_TIME','9:00'),
    ('DEFAULT_TIMEZONE','not/a-zone'),('ALLOW_UNKNOWN_LICENSES','true')])
def test_invalid_environment_fails_early(monkeypatch,env,value):
    monkeypatch.setenv(env,value)
    with pytest.raises(ValueError):
        Settings.from_env(require_bot_token=False)


@pytest.mark.asyncio
async def test_invalid_edition_never_falls_back_to_russian():
    connection=SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    assert await bible.find_translation(connection,'wrong-edition') is None
    assert connection.fetchrow.await_count==1
    assert connection.fetchrow.await_args.args[1]=='wrong-edition'


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards():
    connection=SimpleNamespace(fetch=AsyncMock(return_value=[]))
    await bible.search_verses(connection,123,'10%_'+chr(92),5)
    assert connection.fetch.await_args.args[2]==r'%10\%\_\\%'


def test_all_literal_sql_parameter_counts_match_call_arguments():
    """Static parameter-contract check; deliberately not labelled as PostgreSQL execution."""
    problems=[]
    for path in (ROOT/'app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in {'execute','fetch','fetchval','fetchrow'} and node.args:
                first=node.args[0]
                if isinstance(first,ast.Constant) and isinstance(first.value,str):
                    positions=[int(x) for x in re.findall(r'\$(\d+)',first.value)]
                    if max(positions,default=0)!=len(node.args)-1:
                        problems.append(f'{path.relative_to(ROOT)}:{node.lineno} expected {max(positions,default=0)} args, got {len(node.args)-1}')
    assert not problems,'\n'.join(problems)


def test_no_runtime_secrets_copied_into_image():
    ignores=(ROOT/'.dockerignore').read_text().splitlines()
    assert '.env' in ignores and '.git' in ignores and 'backups' in ignores


class MemoryCheckpoints:
    """In-memory state-machine test adapter; this is NOT a PostgreSQL integration."""
    def __init__(self,chunks):
        self.chunks=tuple(chunks);self.status='pending';self.next=0;self.acks=[];self.progress=0;self.failures=[]
    async def begin(self,envelope):
        if self.status not in {'pending','retry'} or envelope.next_chunk!=self.next:
            return False
        self.status='sending';return True
    async def acknowledge(self,envelope,message_id):
        self.acks.append(message_id);self.next+=1
        self.status='sent' if self.next==len(self.chunks) else 'pending'
        if self.status=='sent':self.progress+=1
    async def fail(self,envelope,kind,retry_after):
        self.status='retry' if kind=='retry' else 'uncertain' if kind=='uncertain' else 'failed'
        self.failures.append((kind,retry_after))
    def envelope(self):
        return Envelope(9,-100,self.chunks,self.next,123)


class ScenarioSender:
    """Deterministic network-outcome adapter without Telegram API calls."""
    def __init__(self,outcomes):self.outcomes=list(outcomes);self.calls=[]
    async def send(self,chat_id,text,thread_id):
        self.calls.append((chat_id,text,thread_id));result=self.outcomes.pop(0)
        if isinstance(result,BaseException):raise result
        return result


@pytest.mark.asyncio
async def test_partial_ack_then_429_retries_only_unconfirmed_chunk():
    store=MemoryCheckpoints(['A','B','C']);sender=ScenarioSender([1,SendError('retry',17),2,3])
    assert await dispatch_chunk(store.envelope(),store,sender)=='partial'
    assert store.progress==0
    assert await dispatch_chunk(store.envelope(),store,sender)=='retry'
    assert store.next==1
    assert await dispatch_chunk(store.envelope(),store,sender)=='partial'
    assert await dispatch_chunk(store.envelope(),store,sender)=='sent'
    assert [x[1] for x in sender.calls]==['A','B','B','C']
    assert all(x[2]==123 for x in sender.calls)
    assert store.acks==[1,2,3] and store.progress==1


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[SendError('uncertain'),TimeoutError(),OSError()])
async def test_ambiguous_send_blocks_automatic_replay(error):
    store=MemoryCheckpoints(['A','B']);sender=ScenarioSender([error])
    assert await dispatch_chunk(store.envelope(),store,sender)=='uncertain'
    assert await dispatch_chunk(store.envelope(),store,sender)=='stale'
    assert len(sender.calls)==1 and store.next==0 and store.progress==0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['forbidden','rejected'])
async def test_permanent_rejection_never_advances_progress(kind):
    store=MemoryCheckpoints(['A']);sender=ScenarioSender([SendError(kind)])
    assert await dispatch_chunk(store.envelope(),store,sender)==kind
    assert store.status=='failed' and store.progress==0


@pytest.mark.asyncio
async def test_database_failure_after_api_ack_keeps_sending_checkpoint():
    store=MemoryCheckpoints(['A']);sender=ScenarioSender([77])
    async def fail_ack(*args):raise RuntimeError('database disconnected')
    store.acknowledge=fail_ack
    with pytest.raises(RuntimeError,match='disconnected'):
        await dispatch_chunk(store.envelope(),store,sender)
    assert store.status=='sending' and store.progress==0
    assert await dispatch_chunk(store.envelope(),store,sender)=='stale'


@pytest.mark.asyncio
async def test_task_cancellation_does_not_turn_into_safe_retry():
    store=MemoryCheckpoints(['A']);sender=ScenarioSender([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await dispatch_chunk(store.envelope(),store,sender)
    assert store.status=='sending' and store.progress==0


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',[0,-1,None,'1',True])
async def test_invalid_transport_ack_is_uncertain(invalid):
    store=MemoryCheckpoints(['A']);sender=ScenarioSender([invalid])
    assert await dispatch_chunk(store.envelope(),store,sender)=='uncertain'
    assert store.progress==0
