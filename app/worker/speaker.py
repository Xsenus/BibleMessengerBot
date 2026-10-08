"""Dedicated speech worker; synthesis never blocks text delivery or image generation."""
import asyncio
import contextlib
import logging

from app.config import Settings
from app.db import acquire_runtime_guard,close_pool,create_pool,wait_for_database
from app.logging import configure_logging
from app.services import speech
from app.services.locks import lock_key

LOGGER=logging.getLogger(__name__)


async def heartbeat(pool):
 while True:
  async with pool.acquire() as c:
   await c.execute("INSERT INTO service_heartbeats(service) VALUES('speaker') ON CONFLICT(service) DO UPDATE SET last_seen=now()")
  await asyncio.sleep(20)


async def worker():
 settings=Settings.from_env(require_bot_token=False)
 audio=speech.SpeechSettings.from_env()
 await wait_for_database(settings)
 pool=await create_pool(settings)
 beat=None
 try:
  async with pool.acquire() as owner:
   await acquire_runtime_guard(owner)
   if not await owner.fetchval('SELECT pg_try_advisory_lock($1)',lock_key('singleton-speaker',1)):
    raise RuntimeError('Another speaker is running')
   await speech.recover(owner)
   beat=asyncio.create_task(heartbeat(pool))
   while True:
    if audio.enabled:
     async with pool.acquire() as c:
      await speech.upgrade_profiles(c,audio)
      # Existing language cards gain audio; arbitrary historical messages are not guessed.
      pending=await c.fetch('SELECT id FROM reading_cards WHERE audio_id IS NULL ORDER BY id LIMIT 100')
      for item in pending:
       await speech.attach(c,item['id'],audio)
      await speech.dispatch(c)
      jobs=await c.fetch("""SELECT a.id FROM reading_audio a WHERE state IN ('queued','retry')
       AND provider_mode=$1 AND (retry_at IS NULL OR retry_at<=now()) AND EXISTS (
        SELECT 1 FROM reading_cards c WHERE c.audio_id=a.id AND c.telegram_message_id IS NOT NULL)
       ORDER BY a.id LIMIT 3""",audio.provider)
      for job in jobs:
       result=await speech.process(c,job['id'],audio)
       LOGGER.info('Speech job %s: %s',job['id'],result)
       await speech.dispatch(c)
    await asyncio.sleep(1)
 finally:
  if beat:
   beat.cancel()
   with contextlib.suppress(asyncio.CancelledError):
    await beat
  await close_pool()


if __name__=='__main__':
 configure_logging()
 asyncio.run(worker())
