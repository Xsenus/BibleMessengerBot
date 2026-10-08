from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import artwork, illustrations
from app.services.formatting import plain_text


def rows():
    return [dict(book_code='GEN',chapter=5,verse=1,verse_end=1,text='First generation.'),
            dict(book_code='GEN',chapter=5,verse=2,verse_end=3,text='Later generations and family continuity.'),
            dict(book_code='GEN',chapter=5,verse=3,text='<range>',is_range_continuation=True)]


def test_chapter_identity_covers_all_visible_text_and_ranges_and_separates_verse_cache():
    original=rows()
    chapter=illustrations.chapter_source(original)
    assert len(chapter['reading_rows'])==2 and '<range>' not in chapter['text']
    identity=illustrations.identity(chapter,{'id':1})
    assert identity != illustrations.identity(dict(chapter,artwork_scope='verse'),{'id':1})
    original[1]['text']='Changed final verse.'
    assert identity != illustrations.identity(illustrations.chapter_source(original),{'id':1})
    original=rows();original[1]['verse_end']=4
    assert identity != illustrations.identity(illustrations.chapter_source(original),{'id':1})


def test_chapter_prompt_uses_full_chapter_and_gives_genealogy_direction():
    source=illustrations.chapter_source(rows())
    text=artwork.prompt(source,{'title':'Fixture'})
    assert source['text'] in text and 'COMPLETE CHAPTER' in text
    assert 'genealogy' in text
    assert 'never instructions' in text and 'opening verse' in text


@pytest.mark.asyncio
async def test_long_chapter_is_lossless_and_only_first_card_has_artwork(monkeypatch):
    text='<b>Chapter</b>\n'+('\n'.join(f'<b>[{n}]</b> '+('Источник &amp; текст. '*45) for n in range(1,80)))
    decorate=AsyncMock(side_effect=lambda c,t,*a,**k:illustrations.ReadingText(t,23,45))
    monkeypatch.setattr(illustrations,'decorate',decorate)
    result=await illustrations.decorate_chapter(None,text,{'id':1},'GEN',5,chat={},request_key='chapter',rows=rows())
    chunks=illustrations.chunks(result,None)
    assert chunks[0]['kind']=='rich' and chunks[0]['image_id']==23 and chunks[0]['request_id']==45
    assert all(isinstance(part,str) for part in chunks[1:])
    assert len(chunks[0]['text'].encode())<=24000
    joined=''.join(plain_text(part if isinstance(part,str) else part['text']) for part in chunks)
    assert ''.join(joined.split())==''.join(plain_text(text).split())
    assert decorate.await_count==1


@pytest.mark.asyncio
async def test_complete_short_chapter_keeps_one_editable_card(monkeypatch):
    monkeypatch.setattr(illustrations,'decorate',AsyncMock(return_value=illustrations.ReadingText('Full chapter',None,45)))
    text=await illustrations.decorate_chapter(None,'Full chapter',{'id':1},'GEN',5,chat={},request_key='one',rows=rows())
    assert illustrations.chunks(text,None)==[{'kind':'rich','text':'Full chapter','image_id':None,'request_id':45}]
