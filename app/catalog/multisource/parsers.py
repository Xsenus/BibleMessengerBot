"""Strict format adapters. Edition-native coordinates are never relabelled as BibleNLP's grid."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.catalog.audit import CORE, NT, OT, CorpusAudit, audit_corpus
from app.catalog.models import DownloadedTranslation, TranslationMeta, VerseReference
from app.catalog.multisource.transport import digest_file
from app.catalog.multisource.types import Candidate, Prepared, Record

ORDER = {book: i for i, book in enumerate(OT + NT)}
# Independent coarse coverage floor, NOT a verse-level proof. Joel/Malachi vary by tradition.
CHAPTER_FLOOR = dict(zip(OT+NT, [50,40,27,36,34,24,21,4,31,24,22,25,29,36,10,13,10,42,150,31,12,8,66,52,5,48,12,14,3,9,1,4,7,3,3,3,2,14,3,
    28,16,24,21,28,16,16,13,6,6,4,4,5,3,6,4,3,1,13,5,5,3,5,1,1,1,22], strict=True))


def positive(value: Any, label: str, *, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdigit()):
        raise ValueError(f"Invalid {label}")
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"{label} out of bounds")
    return number


def book_code(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-4A-Z][A-Z0-9]{2}", value):
        raise ValueError("Unsupported book code; edition was not partially imported")
    return value


def text_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Verse text is not a string")
    if '\ufffd' in value or any(ord(c)<32 and c not in '\n\r\t' for c in value):
        raise ValueError("Invalid Unicode/control character in verse")
    if len(value) > 100000:
        raise ValueError("Verse text is unreasonably large")
    # Preserve internal whitespace and line breaks. No neural rewriting or invented text.
    return value.strip()


def entries(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or not all(isinstance(x, dict) for x in value):
        raise ValueError(f"Missing or malformed {label}")
    return value


def append_verse(records: list[Record], code: str, chapter: int, number: Any, text: Any) -> None:
    """Explicit ranges become one visible anchor and zero-text continuation rows."""
    if isinstance(number, str) and re.fullmatch(r"\d+-\d+", number):
        first, last = map(int, number.split("-"))
        positive(first,"verse"); positive(last,"verse")
        if last < first or last-first>200:
            raise ValueError("Invalid verse range")
    else:
        first = last = positive(number,"verse")
    value = text_value(text)
    if not value:
        return  # missing source verse, never substitute another edition
    records.append((code,chapter,first,value,False,len(records)+1))
    for v in range(first+1,last+1):
        records.append((code,chapter,v,"",True,len(records)+1))


def verify_records(records: list[Record]) -> list[Record]:
    if not records:
        raise ValueError("No readable verses")
    if len(records)>200000:
        raise ValueError("Edition has too many verse rows")
    records = sorted(records,key=lambda r:(ORDER.get(r[0],1000), r[0],r[1],r[2]))
    seen = set()
    previous = None
    for record in records:
        book_code(record[0]); positive(record[1],"chapter"); positive(record[2],"verse")
        key = record[:3]
        if key in seen:
            raise ValueError("Duplicate/overlapping verse coordinate")
        seen.add(key)
        if record[4] and (previous is None or previous[:2]!=record[:2] or previous[2]+1!=record[2]):
            raise ValueError("Orphaned range marker")
        if not record[4] and not text_value(record[3]):
            raise ValueError("Empty visible verse")
        previous = record
    return records


def fingerprint(records: list[Record], numbering: str) -> str:
    h = hashlib.sha256()
    h.update(("BibleMessenger-content-v1\n"+numbering+"\n").encode())
    for row in records:
        h.update((json.dumps(row[:5],ensure_ascii=False,separators=(",",":"))+"\n").encode())
    return h.hexdigest()


def _count(actual: int, advertised: Any, label: str, *, alternate: int | None = None) -> None:
    if advertised is None:
        return
    expected = positive(advertised,label,maximum=200000)
    if actual!=expected and alternate!=expected:
        raise ValueError(f"{label}: actual={actual}, declared={expected}")


def _chapter_sequence(actual: list[int], declared: Any = None, first: int = 1) -> None:
    if len(actual)!=len(set(actual)):
        raise ValueError("Duplicate chapter")
    if not actual or sorted(actual) != list(range(first,first+len(actual))):
        raise ValueError("Missing or out-of-order chapter sequence")
    if declared is not None:
        _count(len(actual),declared,"chapter count")


def native_audit(metadata: TranslationMeta, records: list[Record], numbering: str,
                 declared_chapters: dict[str, list[int]], warnings: list[str]) -> CorpusAudit:
    references = [VerseReference(r[0],r[1],r[2],i+1) for i,r in enumerate(records)]
    base = audit_corpus(metadata,references,records)
    actual = defaultdict(set)
    for code,chapter,_,text,continuation,_ in records:
        if text and not continuation:
            actual[code].add(chapter)
    missing = [f"{code} {chapter}" for code,chapters in declared_chapters.items() for chapter in chapters if chapter not in actual[code]]
    if missing:
        raise ValueError("Downloaded chapters contain no readable text: "+", ".join(missing[:10]))
    nt_rows = sum(1 for r in records if r[0] in NT and not r[4])
    core_rows = sum(1 for r in records if r[0] in CORE and not r[4])
    def covered(books: list[str]) -> bool:
        return all(set(range(1,CHAPTER_FLOOR[b]+1)) <= actual[b] for b in books)
    nt_complete = set(NT)<=set(actual) and covered(NT) and nt_rows>=6000
    full = CORE<=set(actual) and covered(OT+NT) and nt_complete and core_rows>=25000
    ot_complete = set(OT)<=set(actual) and covered(OT) and core_rows-nt_rows>=18000
    scope = ('full' if full else 'nt' if nt_complete and not set(actual).intersection(OT)
             else 'ot' if ot_complete and not set(actual).intersection(NT) else 'partial')
    messages = list(base.warnings)+warnings
    if CORE<=set(actual) and not full:
        messages.append("66 book names alone do not prove full structural coverage")
    return replace(base,coverage=scope,canonical_66_complete=full,nt_complete=nt_complete,
        numbering=numbering,missing_reference_chapters=missing,
        warnings=list(dict.fromkeys(messages)),
        verification_scope="Native source structure/count validation plus independent coarse chapter/density floors; NOT word-for-word or canon certification")


def _measured_metadata(meta: TranslationMeta, records: list[Record]) -> TranslationMeta:
    """Set structural fields missing from catalog from parsed coordinates; originals stay in raw evidence."""
    books=defaultdict(set); verses=Counter()
    for code,chapter,_,_,_,_ in records:
        section='ot' if code in OT else 'nt' if code in NT else 'dc'
        books[section].add(code); verses[section]+=1
    chapters=defaultdict(set)
    for code,chapter,*_ in records:
        chapters['ot' if code in OT else 'nt' if code in NT else 'dc'].add((code,chapter))
    updates={}
    for section in ('ot','nt','dc'):
        updates[section+'_books']=len(books[section]);updates[section+'_chapters']=len(chapters[section]);updates[section+'_verses']=verses[section]
    return replace(meta,**updates)


def prepared_native(candidate: Candidate, path: Path, records: list[Record], names: dict[str,str],
                    chapters: dict[str,list[int]], warnings: list[str], diagnostics: dict[str,Any]) -> Prepared:
    records=verify_records(records)
    meta=_measured_metadata(candidate.metadata,records)
    candidate=replace(candidate,metadata=meta)
    audit=native_audit(meta,records,candidate.numbering,chapters,warnings)
    refs=[VerseReference(r[0],r[1],r[2],i+1) for i,r in enumerate(records)]
    return Prepared(candidate,DownloadedTranslation(meta,path,candidate.url,digest_file(path)),refs,records,
        audit,fingerprint(records,candidate.numbering),names,diagnostics)


def parse_helloao(candidate: Candidate, path: Path, payload: dict, books_payload: dict) -> Prepared:
    identifier=candidate.metadata.translation_id
    for p in (payload,books_payload):
        t=p.get('translation',{})
        if t.get('id')!=identifier or t.get('language')!=candidate.raw.get('language'):
            raise ValueError("HelloAO translation identity/language mismatch")
        if candidate.raw.get('sha256') and t.get('sha256') and candidate.raw['sha256']!=t['sha256']:
            raise ValueError("HelloAO changed during download; retry with --refresh")
    specs={}
    for b in entries(books_payload.get('books'),'book inventory'):
        code=book_code(b.get('id'))
        if code in specs:raise ValueError("Duplicate book in inventory")
        specs[code]=b
    records=[]; names={}; chapters={}; warnings=[]; skipped_annotations=0
    for b in entries(payload.get('books'),'books'):
        code=book_code(b.get('id'))
        if code in chapters or code not in specs:
            raise ValueError("Duplicate/unexpected book in translation")
        spec=specs[code]; names[code]=str(spec.get('name') or code)
        chapter_numbers=[]; before=len(records); visible=0
        for wrapper in entries(b.get('chapters'),'chapters'):
            ch=wrapper.get('chapter',{}); number=positive(ch.get('number'),'chapter')
            chapter_numbers.append(number); start=len(records); count=0
            for v in entries(ch.get('content'),'chapter content'):
                kind=v.get('type')
                if kind=='verse':
                    append_verse(records,code,number,v.get('number'),v.get('text'))
                    count+=1
                elif kind in {'heading','line_break','hebrew_subtitle'}:
                    skipped_annotations+=1
                else:
                    raise ValueError("Unknown HelloAO content type; refusing silent data loss")
            _count(count,wrapper.get('numberOfVerses'),'chapter verse objects',alternate=len(records)-start)
            visible+=count
            if len(records)==start:raise ValueError("Empty HelloAO chapter")
        first=positive(spec.get('firstChapterNumber',1),'first chapter')
        _chapter_sequence(chapter_numbers,spec.get('numberOfChapters'),first)
        if spec.get('lastChapterNumber') is not None and max(chapter_numbers)!=spec['lastChapterNumber']:
            raise ValueError("Last chapter does not match inventory")
        _count(visible,spec.get('totalNumberOfVerses'),'book verse count',alternate=len(records)-before)
        chapters[code]=chapter_numbers
    if set(specs)!=set(chapters):raise ValueError("Complete JSON is missing books from inventory")
    summary=candidate.raw
    # Older API snapshots count apocrypha separately; either documented layout is accepted.
    declared=summary.get('numberOfBooks')
    if declared is not None and len(chapters) not in {int(declared),int(declared)+int(summary.get('numberOfApocryphalBooks',0))}:
        raise ValueError("HelloAO catalogue book count differs from payload")
    for key,actual in [('totalNumberOfChapters',sum(map(len,chapters.values()))),('totalNumberOfVerses',len(records))]:
        value=summary.get(key)
        extra=int(summary.get('totalNumberOfApocryphal'+('Chapters' if key.endswith('Chapters') else 'Verses'),0))
        visible_count=sum(not r[4] for r in records)
        if value and actual not in {int(value),int(value)+extra} and not (key.endswith('Verses') and visible_count in {int(value),int(value)+extra}):
            raise ValueError(f"HelloAO {key} does not match downloaded data")
    if skipped_annotations:
        warnings.append("Headings, subtitles and footnotes are retained in raw cache, not sent as numbered verses")
    return prepared_native(candidate,path,records,names,chapters,warnings,{"skipped_annotation_objects":skipped_annotations,"book_inventory_checked":True})


def getbible_book(number: Any) -> str:
    # The documented 1..66 mapping. Unmapped extra books cause edition rejection, never dropping.
    n=positive(number,'getBible book number')
    if n>66:
        raise ValueError(f"getBible extra-book mapping {n} is not verified; use a USFM-code source for this edition")
    return (OT+NT)[n-1]


def parse_getbible(candidate: Candidate,path: Path,payload: dict,books_payload: dict) -> Prepared:
    if payload.get('abbreviation')!=candidate.metadata.translation_id:
        raise ValueError("getBible edition identity mismatch")
    if candidate.raw.get('sha') and payload.get('sha') and candidate.raw['sha']!=payload['sha']:
        raise ValueError('getBible changed during download; retry with --refresh')
    if payload.get('lang') and payload['lang']!=candidate.raw.get('lang'):
        raise ValueError("getBible language mismatch")
    inventory={}
    if not isinstance(books_payload,dict) or not books_payload:raise ValueError("Missing getBible book inventory")
    for k,b in books_payload.items():
        if not isinstance(b,dict):raise ValueError("Invalid book inventory entry")
        code=getbible_book(b.get('nr',b.get('book_nr',k)))
        if code in inventory:raise ValueError("Duplicate inventory book")
        inventory[code]=b
    records=[];names={};chapters={}
    for b in entries(payload.get('books'),'books'):
        code=getbible_book(b.get('nr',b.get('book_nr')))
        if code in chapters or code not in inventory:raise ValueError("Duplicate/unexpected getBible book")
        names[code]=str(b.get('name') or code);numbers=[]
        for ch in entries(b.get('chapters'),'chapters'):
            number=positive(ch.get('chapter'),'chapter');numbers.append(number);before=len(records)
            for v in entries(ch.get('verses'),'verses'):
                if v.get('chapter',number)!=number:raise ValueError("Verse chapter mismatch")
                append_verse(records,code,number,v.get('verse'),v.get('text'))
            if len(records)==before:raise ValueError("Empty getBible chapter")
        _chapter_sequence(numbers)
        # books.json does not guarantee a chapter-count field; use it when present.
        spec=inventory[code]
        if isinstance(spec.get('chapters'),int):_count(len(numbers),spec['chapters'],'book chapters')
        chapters[code]=numbers
    if set(chapters)!=set(inventory):raise ValueError("Missing books from getBible inventory")
    return prepared_native(candidate,path,records,names,chapters,
        ['getBible inventory confirms books; native omissions are recorded, not repaired with another translation'],
        {'book_inventory_checked':True,'verse_level_external_reference':False})
