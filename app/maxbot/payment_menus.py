"""Refresh delivered main menus after merchant configuration changes, in place."""
from __future__ import annotations

import asyncio
import json
import logging

from app.maxbot.identities import message_external
from app.maxbot.rate_limit import wait_send_slot
from app.maxbot.transport import MaxSender
from app.maxbot.ui import payment_controls
from app.payments.yookassa import merchant_available
from app.services.errors import SendError, UserError
from app.services.locks import chat_lock
from app.maxbot.client import MaxAPIError

LOGGER=logging.getLogger(__name__)


async def refresh(connection,client,bot_id,stop):
    enabled=merchant_available()
    key=f'max:payment-menus:{bot_id}'
    state='enabled' if enabled else 'disabled'
    if await connection.fetchval('SELECT value FROM app_settings WHERE key=$1',key)==state:
        return 0
    cursor,updated,failed=0,0,False
    while not stop.is_set():
        rows=await connection.fetch("""SELECT d.id,d.telegram_chat_id,d.chunks,d.telegram_message_ids,c.ui_language
            FROM delivery_log d JOIN telegram_chats c ON c.telegram_chat_id=d.telegram_chat_id
            WHERE d.id>$1 AND c.platform='max' AND d.status='sent' AND EXISTS(
                SELECT 1 FROM jsonb_array_elements(d.chunks) x WHERE x ? 'max_keyboard'
                AND x->>'kind' IN ('max_text','max_welcome')) ORDER BY d.id LIMIT 50""",cursor)
        if not rows:
            if not failed:
                await connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
                    ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',key,state)
            return updated
        for row in rows:
            if stop.is_set():
                return updated
            chunks=json.loads(row['chunks']) if isinstance(row['chunks'],str) else row['chunks']
            for index,chunk in enumerate(chunks):
                if not isinstance(chunk,dict) or chunk.get('kind') not in {'max_text','max_welcome'}:
                    continue
                keyboard=chunk.get('max_keyboard')
                if not keyboard:
                    continue
                commands={b.get('payload') for line in keyboard['payload']['buttons'] for b in line}
                # Main menus only: preserve bound reading, audio and checkout messages.
                if not {'maxcmd:/today','maxcmd:/help','maxcmd:/daily'}<=commands:
                    continue
                try:
                    async with chat_lock(connection,row['telegram_chat_id']):
                        mid=await message_external(connection,bot_id,row['telegram_chat_id'],row['telegram_message_ids'][index])
                        sender=MaxSender(client,bot_id,connection)
                        attachments=[]
                        if chunk['kind']=='max_welcome':
                            attachments.append({'type':'image','payload':await sender.welcome_media(chunk['asset_sha256'])})
                        if chunk.get('image_id'):
                            attachments.append({'type':'image','payload':await sender.media('image',chunk['image_id'])})
                        markup=payment_controls(keyboard,enabled=enabled,locale=row['ui_language'])
                        if markup:
                            attachments.append(markup)
                        await wait_send_slot(connection,row['telegram_chat_id'])
                        await client.edit(mid,{'text':chunk['text'],'format':'html','attachments':attachments})
                        updated+=1
                except MaxAPIError as error:
                    # Deleted or no-longer-accessible messages do not block all other menus.
                    failed=failed or error.status not in {403,404}
                    LOGGER.warning('MAX payment menu edit deferred (%s)',type(error).__name__)
                except (SendError,UserError,IndexError):
                    failed=True
                    LOGGER.warning('MAX payment menu could not be reconstructed')
            cursor=row['id']
    return updated


async def loop(pool,client,bot_id,stop):
    while not stop.is_set():
        try:
            async with pool.acquire() as connection:
                count=await refresh(connection,client,bot_id,stop)
                if count:
                    LOGGER.info('MAX payment availability updated in %s delivered menus',count)
        except Exception as error:
            LOGGER.warning('MAX payment menu refresh deferred (%s)',type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(),timeout=60)
        except TimeoutError:
            pass
