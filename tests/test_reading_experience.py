"""User-visible prayer/voice contracts and a bounded, intact emergency voice."""
import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import prayers,russian_speech,speech,neural_speech


@pytest.mark.parametrize('chosen',[[],['children'],['justice','environment'],['health'],['peace','gratitude']])
@pytest.mark.parametrize('locale',['ru','en'])
def test_every_prayer_has_all_requested_petitions_once_and_no_visible_provenance(chosen,locale):
    text=prayers.compose(chosen,locale)
    rendered=prayers.invitation({'prayer_text':text,'generator':'local:fixture','news_snapshot':[{'url':'https://news.un.org/en/story/fixture'}]},locale,'09:38')
    assert text.count(prayers.COMMON_PRAYER[locale])==1
    assert all(word in text for word in (['близких','здоровье','церковь','дары','мир','конфликты'] if locale=='ru' else ['loved ones','health','church','gifts','peace','conflicts']))
    assert all(word not in rendered for word in ['news.un.org','Источник','Source','ООН','09:38'])
    assert prayers.with_common_prayer(text,locale)==text


async def test_news_snapshot_survives_ai_failure_and_is_not_shown(monkeypatch):
    news=[{'title':'Help for children','url':'https://news.un.org/en/story/fixture'}]
    monkeypatch.setattr(prayers,'fetch_news',AsyncMock(return_value=news))
    monkeypatch.setattr(prayers,'intentions',AsyncMock(side_effect=ValueError('unavailable')))
    c=SimpleNamespace(fetchrow=AsyncMock(side_effect=[None,{'id':1}]))
    await prayers.brief(c,date(2026,10,9),'UTC','morning_verse','ru')
    args=c.fetchrow.await_args.args
    assert 'Help for children' in args[7]
    assert prayers.COMMON_PRAYER['ru'] in args[5] and args[8]==prayers.COMPOSITION_VERSION


def test_david_and_mary_have_separate_cache_keys_and_numerals_are_spoken():
    david=speech.for_chat(speech.SpeechSettings(),{'audio_voice':'david'})
    mary=speech.for_chat(speech.SpeechSettings(),{'audio_voice':'mary'})
    assert david.russian_voice=='aidar' and mary.russian_voice=='kseniya'
    assert speech.identity('Любовь.','rus',david)!=speech.identity('Любовь.','rus',mary)
    assert russian_speech.spoken_input('Ему было [230] лет.')=='Ему было [двести тридцать] лет.'
    assert speech.spoken_text('<b>Reference</b><i>Editorial context.</i><b>[1]</b> Source text.',{'language_code':'rus','title':'Fixture','license_type':'public-domain'},'ru')=='Source text.'


async def test_primary_voice_failure_uses_preserved_piper_once(monkeypatch):
    calls=[]
    async def run(*args,**kwargs):
        calls.append(args)
        if args[2:3]==('app.services.russian_speech',):
            raise RuntimeError('unavailable')
        return b''
    monkeypatch.setattr(speech,'run_process',run)
    monkeypatch.setattr(speech,'validate_audio',AsyncMock(return_value=(b'ID3'+b'x'*700,5)))
    result=await speech.synthesize('Верный исходный текст.','rus',speech.SpeechSettings())
    assert result[2:] == ('piper',neural_speech.profile('rus'))
    assert sum('app.services.russian_speech' in call for call in calls)==1
    assert sum('app.services.neural_speech' in call for call in calls)==1
    assert not any('espeak-ng' in call for call in calls)


async def test_reserve_can_be_selected_without_loading_primary(monkeypatch):
    run=AsyncMock(return_value=b'')
    monkeypatch.setattr(speech,'run_process',run)
    monkeypatch.setattr(speech,'validate_audio',AsyncMock(return_value=(b'ID3'+b'x'*700,5)))
    result=await speech.synthesize('Текст.','rus',replace(speech.SpeechSettings(),russian_engine='piper'))
    assert result[2]=='piper' and run.await_args_list[0].args[2]=='app.services.neural_speech'


async def test_model_is_verified_before_package_import_and_repairs_corrupt_cache(tmp_path,monkeypatch):
    data=b'checked-official-model'
    model=dict(russian_speech.MODEL,bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(russian_speech,'MODEL',model)
    monkeypatch.setenv('AUDIO_MODELS_PATH',str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=data))) as client:
        await russian_speech.prepare(client=client)
        assert russian_speech.complete(verify=True)
        (russian_speech.directory()/'model.pt').write_bytes(b'x'*len(data))
        with pytest.raises(ValueError,match='not_installed'):
            russian_speech.render('Text.',tmp_path/'out.wav')
        await russian_speech.prepare(client=client)
        assert russian_speech.complete(verify=True)
