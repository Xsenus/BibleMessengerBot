"""Отправка во ВКонтакте: тексты, карточки чтения с картинкой, правка исходных сообщений и аудио (документом mp3)."""
from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image

from app.services.errors import SendError, UserError
from app.vkbot.client import VkAPIError, VkClient
from app.vkbot.identities import external
from app.vkbot.rate_limit import wait_send_slot
from app.vkbot.ui import keyboard, to_text


def image_upload(data: bytes, mime: str) -> tuple[bytes, str, str]:
    """Подготовить картинку для VK: WebP перекодируется в JPEG; допустимы PNG и JPEG. Возвращает (байты, расширение, MIME)."""
    if mime == 'image/webp':
        with Image.open(io.BytesIO(data)) as source:
            buffer = io.BytesIO()
            source.convert('RGB').save(buffer, format='JPEG', quality=95)
            return buffer.getvalue(), 'jpg', 'image/jpeg'
    if mime not in {'image/png', 'image/jpeg'}:
        raise SendError('rejected')
    return data, 'png' if mime == 'image/png' else 'jpg', mime


class VkSender:
    """Отправитель сообщений ВКонтакте: проверяет допустимость материалов, кэширует вложения, ограничивает скорость."""

    def __init__(self, client: VkClient, group_id: int, connection: Any) -> None:
        self.client, self.group_id, self.connection = client, group_id, connection

    async def media(self, kind: str, identifier: int, peer_id: int) -> str:
        """Вложение для картинки (`image`, `asset`) или аудио (`audio`): материал каждый раз проверяется на допустимость;
        загруженное вложение кэшируется в `vk_media`."""
        if type(identifier) is not int or identifier <= 0:
            raise SendError('rejected')
        if kind == 'image':
            asset = await self.connection.fetchrow("SELECT image_data,mime_type FROM verse_illustrations WHERE id=$1 AND status='ready'", identifier)
        elif kind == 'asset':
            asset = await self.connection.fetchrow('SELECT data AS image_data,mime_type FROM media_assets WHERE id=$1', identifier)
        else:
            asset = await self.connection.fetchrow("SELECT audio_data FROM reading_audio WHERE id=$1 AND state='ready'", identifier)
        if not asset:
            raise SendError('rejected')
        cached = await self.connection.fetchval('SELECT attachment FROM vk_media WHERE group_id=$1 AND kind=$2 AND asset_id=$3',
                                                self.group_id, kind, identifier)
        if cached:
            return cached
        try:
            if kind == 'audio':
                attachment = await self.client.upload('doc', peer_id, bytes(asset['audio_data']), f'reading-{identifier}.mp3', 'audio/mpeg')
            else:
                data, suffix, mime = image_upload(bytes(asset['image_data']), asset['mime_type'])
                attachment = await self.client.upload('photo', peer_id, data, f'reading-{identifier}.{suffix}', mime)
        except VkAPIError as error:
            # Загрузка не создаёт видимого сообщения, поэтому безопасно повторить позже.
            raise error.send_error(editing=True) from None
        await self.connection.execute('''INSERT INTO vk_media(group_id,kind,asset_id,attachment) VALUES($1,$2,$3,$4)
            ON CONFLICT(group_id,kind,asset_id) DO UPDATE SET attachment=EXCLUDED.attachment,updated_at=now()''',
                                      self.group_id, kind, identifier, attachment)
        return attachment

    async def welcome_media(self, expected_digest: str, peer_id: int) -> str:
        """Вложение с картинкой приветствия (проверяется хеш файла, загружается один раз)."""
        from app.bot.ui import WELCOME_PATH
        data = WELCOME_PATH.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_digest:
            raise SendError('rejected')
        key = f'vk:welcome:{self.group_id}:{digest}'
        cached = await self.connection.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
        if cached:
            return cached
        try:
            attachment = await self.client.upload('photo', peer_id, data, 'welcome.png', 'image/png')
        except VkAPIError as error:
            raise error.send_error(editing=True) from None
        await self.connection.execute('''INSERT INTO app_settings(key,value) VALUES($1,$2)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', key, attachment)
        return attachment

    async def send(self, chat_id: int, text: str | dict, thread_id: int | None = None, *, reply_markup: Any = None) -> int:
        """Отправить или отредактировать часть сообщения. Потерянное подтверждение отправки — неопределённый исход,
        повтор правки безопасен. Возвращает номер сообщения в беседе (контрольная точка)."""
        editing = isinstance(text, dict) and text.get('kind') == 'rich_edit'
        try:
            if thread_id is not None:
                raise SendError('rejected')                      # тем форума во ВКонтакте нет
            peer_id = await external(self.connection, 'chat', chat_id)
            chat = await self.connection.fetchrow("SELECT ui_language FROM telegram_chats WHERE telegram_chat_id=$1 AND platform='vk'", chat_id)
            if not chat:
                raise SendError('forbidden')
            locale = chat['ui_language']
            chunk = dict(text) if isinstance(text, dict) else None
            attachments: list[str] = []
            audio = None
            keyboard_json = None
            reply_to = None
            if chunk is None:
                body = to_text(str(text))
                keyboard_json = keyboard(reply_markup, locale=locale)
            else:
                kind = chunk.get('kind')
                if kind in {'rich', 'rich_edit'} and chunk.get('card_id'):
                    from app.services.message_languages import outgoing
                    chunk, reply_markup = await outgoing(self.connection, chat_id, chunk)
                if kind == 'vk_audio':
                    card = await self.connection.fetchrow('''SELECT * FROM reading_cards WHERE id=$1 AND telegram_chat_id=$2
                        AND audio_id=$3 AND telegram_message_id IS NOT NULL''', chunk.get('card_id'), chat_id, chunk.get('audio_id'))
                    if not card:
                        raise SendError('rejected')
                    attachments = [await self.media('audio', chunk['audio_id'], peer_id)]
                    body = '🔊 ' + ('Озвучка' if locale == 'ru' else 'Audio reading')
                    reply_to = card['telegram_message_id']
                elif kind in {'rich', 'rich_edit', 'photo', 'asset_photo', 'vk_text', 'vk_welcome'}:
                    if kind == 'vk_welcome':
                        attachments.append(await self.welcome_media(str(chunk.get('asset_sha256')), peer_id))
                    if kind == 'asset_photo':
                        attachments.append(await self.media('asset', int(chunk['asset_id']), peer_id))
                    elif chunk.get('image_id'):
                        attachments.append(await self.media('image', chunk['image_id'], peer_id))
                    content = chunk.get('caption', '') if kind in {'photo', 'asset_photo'} else chunk.get('text')
                    if not isinstance(content, str):
                        raise SendError('rejected')
                    body = to_text(content)
                    if chunk.get('card_id') and chunk.get('audio_id'):
                        audio = (chunk['card_id'], chunk['audio_id'])
                    keyboard_json = chunk.get('vk_keyboard') or keyboard(reply_markup, audio=audio, locale=locale)
                else:
                    raise SendError('rejected')
            await wait_send_slot(self.connection, chat_id)
            if editing:
                assert chunk is not None
                await self.client.edit(peer_id, chunk['message_id'], message=body, attachment=','.join(attachments) or None,
                                       keyboard=keyboard_json)
                if audio:
                    await self.connection.execute('UPDATE reading_cards SET audio_offered_id=$2 WHERE id=$1', audio[0], audio[1])
                return chunk['message_id']
            cmid = await self.client.send(peer_id, message=body, attachment=','.join(attachments) or None,
                                          keyboard=keyboard_json, reply_to=reply_to)
            if audio:
                await self.connection.execute('UPDATE reading_cards SET audio_offered_id=$2 WHERE id=$1', audio[0], audio[1])
            if chunk and chunk.get('kind') == 'vk_audio':
                from app.services.speech import acknowledged
                await acknowledged(self.connection, chunk['card_id'], chunk['audio_id'])
            return cmid
        except VkAPIError as error:
            raise error.send_error(editing=editing) from None
        except UserError:
            raise SendError('forbidden') from None
        except (ValueError, KeyError, TypeError):
            raise SendError('rejected') from None

