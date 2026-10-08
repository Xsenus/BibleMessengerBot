"""Shared asynchronous reading audio; free by default, paid requests require opt-in."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.services import bible
from app.services.formatting import plain_text
from app.services.i18n import ui_for_language
from app.services.locks import chat_lock, lock_key

LOGGER = logging.getLogger(__name__)
MAX_BYTES = 20 * 1024**2
MAX_CACHE_BYTES = 1024**3
EDGE_VOICES = {
 'rus':'ru-RU-DmitryNeural', 'eng':'en-US-GuyNeural', 'ell':'el-GR-NestorasNeural',
 'hbo':'he-IL-AvriNeural','heb':'he-IL-AvriNeural', 'deu':'de-DE-ConradNeural',
 'fra':'fr-FR-HenriNeural', 'ita':'it-IT-DiegoNeural', 'spa':'es-ES-AlvaroNeural',
 'ukr':'uk-UA-OstapNeural','zho':'zh-CN-XiaoxiaoNeural','cmn':'zh-CN-XiaoxiaoNeural',
 'por':'pt-BR-AntonioNeural','pol':'pl-PL-MarekNeural','ara':'ar-SA-HamedNeural',
 'arb':'ar-SA-HamedNeural','jpn':'ja-JP-KeitaNeural','kor':'ko-KR-InJoonNeural',
 'tur':'tr-TR-AhmetNeural','nld':'nl-NL-MaartenNeural','hin':'hi-IN-MadhurNeural',
 'ind':'id-ID-ArdiNeural','vie':'vi-VN-NamMinhNeural','ces':'cs-CZ-AntoninNeural',
 'ron':'ro-RO-EmilNeural','swe':'sv-SE-MattiasNeural','fas':'fa-IR-FaridNeural',
}
LOCAL_VOICES = {'grc':'grc','lat':'la','hbo':'he','heb':'he','zho':'cmn','cmn':'cmn'}


@dataclass(frozen=True)
class SpeechSettings:
 enabled: bool = True
 provider: str = 'free'
 key: str = field(default='',repr=False)
 model: str = 'gpt-4o-mini-tts'
 voice: str = 'marin'
 paid_enabled: bool = False
 monthly_characters: int = 0
 daily_characters: int = 0

 @classmethod
 def from_env(cls):
  provider=os.getenv('AUDIO_PROVIDER','free')
  if provider not in {'free','neural','edge','espeak','openai'}:
   raise ValueError('Invalid AUDIO_PROVIDER')
  monthly=int(os.getenv('AUDIO_PAID_MONTHLY_MAX_CHARACTERS','0'))
  daily=int(os.getenv('AUDIO_PAID_DAILY_MAX_CHARACTERS','0'))
  if min(monthly,daily)<0:
   raise ValueError('Negative audio limit')
  return cls(enabled=os.getenv('AUDIO_ENABLED','true').lower() in {'true','1','yes'},
   provider=provider,key=os.getenv('AUDIO_OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY',''),
   model=os.getenv('AUDIO_OPENAI_MODEL','gpt-4o-mini-tts'),voice=os.getenv('AUDIO_OPENAI_VOICE','marin'),
   paid_enabled=os.getenv('AUDIO_PAID_ENABLED','false').lower() in {'true','1','yes'},
   monthly_characters=monthly,daily_characters=daily)


def spoken_text(html, edition, locale):
 """Read the displayed source, excluding headings, verse labels and provenance."""
 for language in {locale,ui_for_language(edition['language_code']) or locale}:
  footer=bible.attribution(edition,language)
  if html.endswith(footer):
   html=html[:-len(footer)]
   break
 html=re.sub(r'<b>.*?</b>','',html,flags=re.S)
 return re.sub(r'\s+',' ',plain_text(html)).strip()


def voice_profile(language, settings):
 from app.services import neural_speech
 return neural_speech.profile(language) if settings.provider in {'free','neural'} and language in neural_speech.LANGUAGES else 'v1'


def identity(text, language, settings):
 mode=settings.provider
 signature=f'{voice_profile(language,settings)}:{mode}:{settings.model if mode=="openai" else ""}:{settings.voice if mode=="openai" else ""}:{language}\0{text}'
 return hashlib.sha256(signature.encode()).hexdigest()


async def attach(connection, card_id, settings=None):
 """Freeze the actual visible page and language without waiting for synthesis."""
 from app.services.message_languages import pages
 settings=settings or SpeechSettings.from_env()
 if not settings.enabled:
  return None
 card=await connection.fetchrow('SELECT * FROM reading_cards WHERE id=$1',card_id)
 edition=await bible.find_translation(connection,card['selected_translation_id'])
 if not edition:
  return None
 content=pages(card['current_html'])
 text=spoken_text(content[min(card['text_page'],len(content)-1)],edition,card['ui_language'])
 if not text:
  return None
 identifier=await ensure_audio(connection,text,edition['language_code'],settings)
 await connection.execute('UPDATE reading_cards SET audio_id=$2 WHERE id=$1',card_id,identifier)
 return identifier


async def ensure_audio(connection, text, language, settings):
 key=identity(text,language,settings)
 return await connection.fetchval('''INSERT INTO reading_audio(cache_key,language_code,source_text,provider_mode,voice_profile)
  VALUES($1,$2,$3,$4,$5) ON CONFLICT(cache_key) DO UPDATE SET cache_key=EXCLUDED.cache_key RETURNING id''',
  key,language,text,settings.provider,voice_profile(language,settings))


async def media(connection, card):
 if not card.get('audio_id') or not SpeechSettings.from_env().enabled:
  return None
 return await connection.fetchrow("SELECT * FROM reading_audio WHERE id=$1 AND state='ready'",card['audio_id'])


async def acknowledged(connection, card_id, audio_id):
 await connection.execute('UPDATE reading_cards SET audio_sent_id=$2 WHERE id=$1',card_id,audio_id)


async def run_process(*args, stdin=None, timeout=180):
 process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,
  stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 try:
  output,error=await asyncio.wait_for(process.communicate(stdin),timeout)
 except BaseException:
  if process.returncode is None:
   process.kill()
  await process.wait()
  raise
 if process.returncode:
  raise RuntimeError('speech_process_failed')  # Do not log speech text or provider credentials.
 return output


def portions(text, maximum=3000):
 """Lossless bounded inputs, including chapters longer than a provider request."""
 result=[]
 while text:
  cut=min(len(text),maximum)
  if cut<len(text):
   candidate=text.rfind(' ',0,cut)
   if candidate>maximum//2:
    cut=candidate+1
  result.append(text[:cut]);text=text[cut:]
 return result


async def validate_audio(path):
 if not path.is_file() or not 512<=path.stat().st_size<=MAX_BYTES:
  raise ValueError('invalid_audio_size')
 raw=await run_process('ffprobe','-v','error','-select_streams','a:0',
  '-show_entries','stream=codec_type:format=duration','-of','json',str(path),timeout=15)
 info=json.loads(raw)
 if not any(s.get('codec_type')=='audio' for s in info.get('streams',[])):
  raise ValueError('invalid_audio_stream')
 duration=float(info['format']['duration'])
 if not 0<duration<=3600:
  raise ValueError('invalid_audio_duration')
 return path.read_bytes(),max(1,round(duration))


async def synthesize(text, language, settings, *, client=None):
 """Return validated MP3 bytes, duration and exact provider/voice provenance."""
 with tempfile.TemporaryDirectory(prefix='bible-audio-') as directory:
  root=Path(directory);output=root/'reading.mp3'
  from app.services import neural_speech
  if settings.provider in {'free','neural'} and language in neural_speech.LANGUAGES:
   wav=root/'reading.wav'
   await run_process(sys.executable,'-m','app.services.neural_speech',language,str(wav),stdin=text.encode(),timeout=600)
   await run_process('ffmpeg','-v','error','-i',str(wav),'-threads','1','-ac','1','-ar','24000',
    '-codec:a','libmp3lame','-b:a','96k',str(output))
   data,duration=await validate_audio(output)
   return data,duration,neural_speech.MODELS[language]['engine'],voice_profile(language,settings)
  if settings.provider=='neural':
   raise ValueError('unsupported_neural_language')
  if settings.provider=='openai':
   if not settings.paid_enabled or not settings.key or min(settings.monthly_characters,settings.daily_characters)<=0:
    raise ValueError('paid_audio_disabled')
   parts=[]
   async with httpx.AsyncClient(timeout=90,follow_redirects=False) if client is None else _client(client) as api:
    for index,piece in enumerate(portions(text)):
     async with api.stream('POST','https://api.openai.com/v1/audio/speech',
      headers={'Authorization':'Bearer '+settings.key},json=dict(model=settings.model,voice=settings.voice,
       input=piece,response_format='mp3',instructions='Read this text verbatim in its original language. Calm, clear narration. Do not translate or add words.')) as response:
      response.raise_for_status()
      target=root/f'part-{index}.mp3';size=0
      with target.open('wb') as stream:
       async for data in response.aiter_bytes():
        size+=len(data)
        if size>MAX_BYTES:
         raise ValueError('audio_too_large')
        stream.write(data)
     parts.append(target)
   playlist=root/'parts.txt'
   playlist.write_text(''.join(f"file '{part.name}'\n" for part in parts))
   await run_process('ffmpeg','-v','error','-f','concat','-safe','1','-i',str(playlist),
    '-threads','1','-ac','1','-ar','24000','-codec:a','libmp3lame','-b:a','64k',str(output))
   data,duration=await validate_audio(output)
   return data,duration,'openai',settings.voice
  if settings.provider in {'free','edge'} and language in EDGE_VOICES:
   try:
    import edge_tts
    async def save():
     size=0
     with output.open('wb') as stream:
      async for item in edge_tts.Communicate(text,EDGE_VOICES[language],rate='-10%',receive_timeout=30).stream():
       if item['type']=='audio':
        size+=len(item['data'])
        if size>MAX_BYTES:
         raise ValueError('audio_too_large')
        stream.write(item['data'])
    await asyncio.wait_for(save(),240)
    data,duration=await validate_audio(output)
    return data,duration,'edge',EDGE_VOICES[language]
   except Exception as error:
    if settings.provider=='edge':
     raise
    LOGGER.warning('Free neural speech unavailable (%s); using local voice',type(error).__name__)
  elif settings.provider=='edge':
   raise ValueError('unsupported_edge_language')
  voice=LOCAL_VOICES.get(language) or ui_for_language(language)
  if not voice:
   raise ValueError('unsupported_speech_language')
  # Explicitly verify the requested language: eSpeak must never silently read in English.
  voices=await run_process('espeak-ng','--voices='+voice,timeout=10)
  if not any(len(line.split())>1 and line.split()[1].decode()==voice for line in voices.splitlines()[1:]):
   raise ValueError('unsupported_local_voice')
  wav=root/'reading.wav'
  await run_process('espeak-ng','-v',voice,'-s','145','-w',str(wav),'--stdin',stdin=text.encode())
  output.unlink(missing_ok=True)
  await run_process('ffmpeg','-v','error','-i',str(wav),'-threads','1','-ac','1','-ar','24000',
   '-codec:a','libmp3lame','-b:a','64k',str(output))
  data,duration=await validate_audio(output)
  return data,duration,'espeak',voice


@asynccontextmanager
async def _client(client):
 yield client


async def reserve_paid(connection, audio, settings):
 if not settings.paid_enabled or not settings.key or min(settings.monthly_characters,settings.daily_characters)<=0:
  return None
 characters=len(audio['source_text'])
 async with connection.transaction():
  await connection.execute('SELECT pg_advisory_xact_lock($1)',lock_key('audio-paid-budget',1))
  counts=await connection.fetchrow("""SELECT COALESCE(sum(characters),0) AS month,
   COALESCE(sum(characters) FILTER (WHERE created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),0) AS day
   FROM audio_generation_attempts WHERE created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""")
  if counts['month']+characters>settings.monthly_characters or counts['day']+characters>settings.daily_characters:
   return None
  return await connection.fetchval("INSERT INTO audio_generation_attempts(audio_id,characters,state) VALUES($1,$2,'running') RETURNING id",audio['id'],characters)


async def recover(connection):
 await connection.execute("UPDATE audio_generation_attempts SET state='uncertain',updated_at=now() WHERE state='running'")
 await connection.execute("""UPDATE reading_audio SET state=CASE WHEN provider_mode='openai' THEN 'uncertain' ELSE 'retry' END,
  retry_at=now(),error_code='worker_interrupted',updated_at=now() WHERE state='running'""")


async def process(connection, identifier, settings=None, *, generator=synthesize):
 settings=settings or SpeechSettings.from_env()
 if not settings.enabled:
  return 'disabled'
 lock=lock_key('audio-job',identifier)
 if not await connection.fetchval('SELECT pg_try_advisory_lock($1)',lock):
  return 'busy'
 attempt=None
 try:
  audio=await connection.fetchrow("""SELECT * FROM reading_audio WHERE id=$1 AND state IN ('queued','retry')
   AND (retry_at IS NULL OR retry_at<=now())""",identifier)
  if not audio:
   return 'not_due'
  if audio['provider_mode']!=settings.provider or audio['voice_profile']!=voice_profile(audio['language_code'],settings):
   return 'configuration_changed'
  from app.services.scheduled_media import AUDIO_ELIGIBILITY
  if not await connection.fetchval('SELECT '+AUDIO_ELIGIBILITY+' FROM reading_audio a WHERE a.id=$1',identifier):
   return 'unbound'
  if await connection.fetchval("SELECT COALESCE(sum(octet_length(audio_data)),0) FROM reading_audio")>=MAX_CACHE_BYTES:
   return 'storage_limit'
  if settings.provider=='openai':
   attempt=await reserve_paid(connection,audio,settings)
   if not attempt:
    return 'paid_disabled_or_limit'
  await connection.execute("UPDATE reading_audio SET state='running',attempts=attempts+1,updated_at=now() WHERE id=$1",identifier)
  try:
   data,duration,provider,voice=await generator(audio['source_text'],audio['language_code'],settings)
   if not 512<=len(data)<=MAX_BYTES or not 0<duration<=3600:
    raise ValueError('invalid_audio_result')
   async with connection.transaction():
    await connection.execute('SELECT pg_advisory_xact_lock($1)',lock_key('audio-storage',1))
    stored=await connection.fetchval('SELECT COALESCE(sum(octet_length(audio_data)),0) FROM reading_audio')
    if stored+len(data)>MAX_CACHE_BYTES:
     raise ValueError('audio_storage_limit')
    await connection.execute("""UPDATE reading_audio SET state='ready',audio_data=$2,duration=$3,provider=$4,voice=$5,
     error_code=NULL,retry_at=NULL,updated_at=now() WHERE id=$1""",identifier,data,duration,provider,voice)
    if attempt:
     await connection.execute("UPDATE audio_generation_attempts SET state='ready',updated_at=now() WHERE id=$1",attempt)
   return 'ready'
  except Exception as error:
   # Paid ambiguous responses retain their reservation and are never automatically repeated.
   status='uncertain' if attempt else 'retry' if audio['attempts']<3 else 'failed'
   code=type(error).__name__
   async with connection.transaction():
    await connection.execute("""UPDATE reading_audio SET state=$2,error_code=$3,retry_at=now()+interval '5 minutes',
     updated_at=now() WHERE id=$1""",identifier,status,code)
    if attempt:
     await connection.execute("UPDATE audio_generation_attempts SET state='uncertain',updated_at=now() WHERE id=$1",attempt)
   return status
 finally:
  await connection.execute('SELECT pg_advisory_unlock($1)',lock)


async def upgrade_profiles(connection, settings=None):
 """Rebind old cached voices without discarding their media or changing selections."""
 from app.services import neural_speech
 settings=settings or SpeechSettings.from_env()
 if not settings.enabled or settings.provider not in {'free','neural'}:
  return 0
 profiles={language:voice_profile(language,settings) for language in neural_speech.LANGUAGES}
 cards=await connection.fetch('''SELECT c.id,c.telegram_chat_id FROM reading_cards c
  JOIN reading_audio a ON a.id=c.audio_id JOIN jsonb_each_text($1::jsonb) p ON p.key=a.language_code
  WHERE a.provider_mode=$2 AND a.voice_profile<>p.value ORDER BY c.id LIMIT 100''',json.dumps(profiles),settings.provider)
 for card in cards:
  async with chat_lock(connection,card['telegram_chat_id']),connection.transaction():
   await attach(connection,card['id'],settings)
 return len(cards)


async def dispatch(connection):
 """Fan out shared ready audio to exact acknowledged cards, using their latest language."""
 from app.worker.delivery import insert_payload
 if not SpeechSettings.from_env().enabled:
  return 0
 cards=await connection.fetch("""SELECT c.id,c.telegram_chat_id FROM reading_cards c JOIN reading_audio a ON a.id=c.audio_id
  JOIN telegram_chats t ON t.telegram_chat_id=c.telegram_chat_id AND t.is_active
  WHERE a.state='ready' AND c.telegram_message_id IS NOT NULL AND c.audio_sent_id IS DISTINCT FROM c.audio_id
  ORDER BY c.id LIMIT 100""")
 count=0
 for hint in cards:
  async with chat_lock(connection,hint['telegram_chat_id']),connection.transaction():
   card=await connection.fetchrow('SELECT * FROM reading_cards WHERE id=$1 FOR UPDATE',hint['id'])
   if not await media(connection,card) or card['audio_id']==card['audio_sent_id']:
    continue
   chat=await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1',card['telegram_chat_id'])
   edition=await bible.find_translation(connection,card['source_translation_id'])
   if not edition or not chat['is_active']:
    continue
   await insert_payload(connection,chat,edition,card['current_html'],f"audio:{card['id']}:{card['audio_id']}",
    'illustration_edit',{'kind':'illustration_edit'},frozen_chunks=[dict(kind='rich_edit',card_id=card['id'],
      message_id=card['telegram_message_id'],text=card['current_html'],image_id=card['image_id'])])
   count+=1
 return count
