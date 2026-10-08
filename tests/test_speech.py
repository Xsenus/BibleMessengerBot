"""Speech adapters: verbatim inputs, bounded media, free fallback and opt-in paid API."""
import json
import shutil
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import speech
from tests.test_reading_presentation import edition


def test_source_extraction_keeps_scripture_numbers_and_escapes():
 value=edition()
 text='<b>Бытие 5</b>\n<b>[1]</b> Было [230] лет &amp; мир.\n<b>[2]</b> Другой стих.\n\n'+speech.bible.attribution(value,'ru')
 assert speech.spoken_text(text,value,'ru')=='Было [230] лет & мир. Другой стих.'
 text='<blockquote>Точные &lt;слова&gt;.</blockquote>\n<b>Бытие 1:1</b>\n'+speech.bible.attribution(value,'ru')
 assert speech.spoken_text(text,value,'ru')=='Точные <слова>.'


def test_lossless_provider_chunks_and_language_cache_separation():
 text=('Long text!  Ελληνική речь. '*400)+'Finish.'
 parts=speech.portions(text)
 assert ''.join(parts)==text and all(0<len(part)<=3000 for part in parts)
 settings=speech.SpeechSettings()
 assert speech.identity(text,'rus',settings)!=speech.identity(text,'grc',settings)
 assert speech.identity(text,'rus',settings)==speech.identity(text,'rus',settings)
 assert 'grc' not in speech.EDGE_VOICES and speech.LOCAL_VOICES['grc']=='grc'


def test_paid_defaults_are_closed(monkeypatch):
 for key in ['AUDIO_PROVIDER','AUDIO_PAID_ENABLED','AUDIO_PAID_MONTHLY_MAX_CHARACTERS','AUDIO_PAID_DAILY_MAX_CHARACTERS']:
  monkeypatch.delenv(key,raising=False)
 monkeypatch.setenv('OPENAI_API_KEY','secret-test-key')
 settings=speech.SpeechSettings.from_env()
 assert settings.provider=='free' and not settings.paid_enabled
 assert settings.monthly_characters==settings.daily_characters==0
 assert 'secret-test-key' not in repr(settings)
 monkeypatch.setenv('AUDIO_PROVIDER','unknown')
 with pytest.raises(ValueError):
  speech.SpeechSettings.from_env()


@pytest.mark.asyncio
async def test_paid_adapter_never_calls_api_without_opt_in():
 transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('Paid request forbidden')))
 async with httpx.AsyncClient(transport=transport) as client:
  with pytest.raises(ValueError,match='paid_audio_disabled'):
   await speech.synthesize('Текст.','rus',speech.SpeechSettings(provider='openai',key='fixture'),client=client)


@pytest.mark.asyncio
async def test_paid_adapter_chunks_exact_input_and_checks_audio(monkeypatch):
 captured=[]
 def response(request):
  captured.append(json.loads(request.content))
  return httpx.Response(200,content=b'ID3'+b'fixture'*100,headers={'Content-Type':'audio/mpeg'})
 async def validate(path):
  return b'validated'*100,25
 monkeypatch.setattr(speech,'run_process',AsyncMock(return_value=b''))
 monkeypatch.setattr(speech,'validate_audio',validate)
 settings=speech.SpeechSettings(provider='openai',key='fixture',paid_enabled=True,monthly_characters=100000,daily_characters=100000)
 text='Точный текст без изменений. '*300
 async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
  data,duration,provider,voice=await speech.synthesize(text,'rus',settings,client=client)
 assert ''.join(item['input'] for item in captured)==text
 assert all(len(item['input'])<=3000 and item['model']=='gpt-4o-mini-tts' for item in captured)
 assert (duration,provider,voice)==(25,'openai','marin') and data


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which('espeak-ng') or not shutil.which('ffmpeg'),reason='Local speech tools required')
@pytest.mark.parametrize('language,text',[('rus','В начале сотворил Бог небо и землю.'),('grc','Ἐν ἀρχῇ ἦν ὁ λόγος.'),('lat','In principio erat Verbum.')])
async def test_real_free_local_audio_decodes(language,text):
 data,duration,provider,voice=await speech.synthesize(text,language,speech.SpeechSettings(provider='espeak'))
 assert len(data)>512 and duration>0 and provider=='espeak'
 assert voice==speech.LOCAL_VOICES.get(language,language[:2])


@pytest.mark.asyncio
async def test_empty_and_non_audio_results_are_rejected(tmp_path,monkeypatch):
 with pytest.raises(ValueError,match='size'):
  await speech.validate_audio(tmp_path/'missing.mp3')
 path=tmp_path/'invalid.mp3';path.write_bytes(b'x'*600)
 monkeypatch.setattr(speech,'run_process',AsyncMock(return_value=json.dumps({'streams':[],'format':{'duration':'10'}}).encode()))
 with pytest.raises(ValueError,match='stream'):
  await speech.validate_audio(path)


@pytest.mark.asyncio
async def test_free_neural_failure_falls_back_without_calling_paid_api(monkeypatch):
 import edge_tts
 class Unavailable:
  def __init__(self,*args,**kwargs):
   pass
  async def stream(self):
   raise TimeoutError()
   yield None
 monkeypatch.setattr(edge_tts,'Communicate',Unavailable)
 run=AsyncMock(return_value=b'Pty Language Name\n5 ru Russian\n')
 monkeypatch.setattr(speech,'run_process',run)
 monkeypatch.setattr(speech,'validate_audio',AsyncMock(return_value=(b'ID3'+b'a'*600,7)))
 result=await speech.synthesize('Русский текст.','rus',speech.SpeechSettings())
 assert result[2:]==('espeak','ru')
 assert any(call.args[0]=='espeak-ng' and '--stdin' in call.args for call in run.await_args_list)
 assert speech.SpeechSettings().paid_enabled is False
