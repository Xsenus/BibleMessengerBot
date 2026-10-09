"""MAX cards, original-message edits and MP3 using shared approved media assets."""
from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from PIL import Image

from app.maxbot.client import MaxAPIError, MaxClient
from app.maxbot.identities import external, message_external, remember_message
from app.maxbot.rate_limit import wait_send_slot
from app.maxbot.ui import keyboard_attachment
from app.services.errors import SendError, UserError


def image_upload(data: bytes, mime: str) -> tuple[bytes, str, str]:
    """MAX lacks WebP uploads; transcode approved bytes without altering the DB asset."""
    if mime == 'image/webp':
        with Image.open(io.BytesIO(data)) as source:
            buffer = io.BytesIO()
            source.convert('RGB').save(buffer, format='JPEG', quality=95)
            return buffer.getvalue(), 'jpg', 'image/jpeg'
    if mime not in {'image/png', 'image/jpeg'}:
        raise SendError('rejected')
    return data, 'png' if mime == 'image/png' else 'jpg', mime


class MaxSender:
    """Use positive local message checkpoints and bot-scoped MAX upload tokens."""

    def __init__(self, client: MaxClient, bot_id: int, connection: Any):
        self.client, self.bot_id, self.connection = client, bot_id, connection

    async def media(self, kind: str, identifier: int) -> dict:
        if type(identifier) is not int or identifier <= 0:
            raise SendError('rejected')
        # Check asset approval on every reuse: a cached token cannot revive quarantine.
        if kind == 'image':
            asset = await self.connection.fetchrow("SELECT image_data,mime_type FROM verse_illustrations WHERE id=$1 AND status='ready'", identifier)
        else:
            asset = await self.connection.fetchrow("SELECT audio_data FROM reading_audio WHERE id=$1 AND state='ready'", identifier)
        if not asset:
            raise SendError('rejected')
        cached = await self.connection.fetchval('SELECT payload FROM max_media WHERE bot_id=$1 AND kind=$2 AND asset_id=$3', self.bot_id, kind, identifier)
        if cached:
            return json.loads(cached) if isinstance(cached, str) else cached
        if kind == 'image':
            data, suffix, mime = image_upload(bytes(asset['image_data']), asset['mime_type'])
        else:
            data, suffix, mime = bytes(asset['audio_data']), 'mp3', 'audio/mpeg'
        try:
            payload = await self.client.upload(kind, data, f'reading-{identifier}.{suffix}', mime)
        except MaxAPIError as error:
            # Media preparation has not sent a visible message yet. A safe retry
            # does not repeat a delivered reading or another paid generation.
            raise error.send_error(editing=True) from None
        await self.connection.execute('''INSERT INTO max_media(bot_id,kind,asset_id,payload)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(bot_id,kind,asset_id) DO UPDATE
            SET payload=EXCLUDED.payload,updated_at=now()''', self.bot_id, kind, identifier, json.dumps(payload))
        return payload

    async def welcome_media(self, expected_digest: str) -> dict:
        from app.bot.ui import WELCOME_PATH
        data = WELCOME_PATH.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_digest:
            raise SendError('rejected')
        key = f'max:welcome:{self.bot_id}:{digest}'
        cached = await self.connection.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
        if cached:
            return json.loads(cached)
        try:
            payload = await self.client.upload('image', data, 'welcome.png', 'image/png')
        except MaxAPIError as error:
            raise error.send_error(editing=True) from None
        await self.connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', key, json.dumps(payload))
        return payload

    async def send(self, chat_id: int, text: str | dict, thread_id: int | None = None,
                   *, reply_markup: Any = None) -> int:
        """A lost send ACK is uncertain; retrying a fixed original edit is safe."""
        editing = isinstance(text, dict) and text.get('kind') == 'rich_edit'
        try:
            if thread_id is not None:
                raise SendError('rejected')  # MAX has no Telegram forum topics API.
            external_chat = await external(self.connection, 'chat', chat_id)
            chat = await self.connection.fetchrow("SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='max'", chat_id)
            if not chat:
                raise SendError('forbidden')
            locale = chat['ui_language']
            audio = None
            attachments = []
            chunk = dict(text) if isinstance(text, dict) else None
            if chunk:
                kind = chunk.get('kind')
                if kind in {'rich', 'rich_edit'} and chunk.get('card_id'):
                    from app.services.message_languages import outgoing
                    chunk, reply_markup = await outgoing(self.connection, chat_id, chunk)
                if kind == 'max_audio':
                    # Authorization of the card/message happens again at send time.
                    card = await self.connection.fetchrow('''SELECT * FROM reading_cards
                        WHERE id=$1 AND telegram_chat_id=$2 AND audio_id=$3 AND telegram_message_id IS NOT NULL''',
                        chunk.get('card_id'), chat_id, chunk.get('audio_id'))
                    if not card:
                        raise SendError('rejected')
                    attachments = [{'type': 'audio', 'payload': await self.media('audio', chunk['audio_id'])}]
                    body = {'attachments': attachments}
                elif kind in {'rich', 'rich_edit', 'photo', 'max_text', 'max_welcome'}:
                    if kind == 'max_welcome':
                        attachments.append({'type': 'image', 'payload': await self.welcome_media(chunk.get('asset_sha256'))})
                    if chunk.get('image_id'):
                        attachments.append({'type': 'image', 'payload': await self.media('image', chunk['image_id'])})
                    content = chunk.get('caption', '') if kind == 'photo' else chunk.get('text')
                    if not isinstance(content, str):
                        raise SendError('rejected')
                    if chunk.get('card_id') and chunk.get('audio_id'):
                        audio = (chunk['card_id'], chunk['audio_id'])
                    body = {'text': content or None, 'format': 'html', 'attachments': attachments}
                else:
                    raise SendError('rejected')
            else:
                body = {'text': str(text), 'format': 'html', 'attachments': attachments}
            if not chunk or chunk.get('kind') != 'max_audio':
                keyboard = chunk.get('max_keyboard') if chunk and chunk.get('max_keyboard') else keyboard_attachment(reply_markup, audio=audio, locale=locale)
                if keyboard:
                    attachments.append(keyboard)
            target = chunk.get('message_id') if editing else None
            external_message = await message_external(self.connection, self.bot_id, chat_id, target) if editing else None
            await wait_send_slot(self.connection, chat_id)
            if editing:
                await self.client.edit(external_message, body)
                if audio:
                    await self.connection.execute('UPDATE reading_cards SET audio_offered_id=$2 WHERE id=$1', *audio)
                return target
            mid = await self.client.send(chat_id=external_chat, body=body)
            checkpoint = await remember_message(self.connection, self.bot_id, chat_id, mid)
            if not checkpoint:
                raise SendError('uncertain')
            if audio:
                await self.connection.execute('UPDATE reading_cards SET audio_offered_id=$2 WHERE id=$1', *audio)
            if chunk and chunk.get('kind') == 'max_audio':
                from app.services.speech import acknowledged
                await acknowledged(self.connection, chunk['card_id'], chunk['audio_id'])
            return checkpoint
        except MaxAPIError as error:
            raise error.send_error(editing=editing) from None
        except UserError:
            raise SendError('forbidden') from None
        except ValueError:
            raise SendError('rejected') from None
