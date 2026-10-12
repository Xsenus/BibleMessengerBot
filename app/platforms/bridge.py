"""Базовый мост платформы: имитация бота aiogram для общих обработчиков команд.

Общие обработчики (`app.bot.handlers`) написаны под aiogram. Мост подставляет вместо `Bot` объект с теми же методами
(`get_me`, `get_chat`, `get_chat_member`, ответ на нажатие) и ставит ответы в общую очередь доставки. Наследник реализует только
то, что зависит от платформы: проверку чата и прав, подтверждение нажатия, кнопки и вид частей текста."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.platforms.spec import PlatformSpec
from app.services import bible, illustrations, message_languages
from app.services.errors import UserError
from app.worker.delivery import insert_payload


@dataclass(slots=True)
class ReplyContext:
    """Контекст ответов на одно событие: ключ события и счётчик ответов (для стабильных ключей идемпотентности)."""
    key: str
    position: int = 0


class BridgeBase:
    """Общая часть мостов. Наследник задаёт `spec` и переопределяет перечисленные ниже методы."""
    spec: PlatformSpec

    def __init__(self, client: Any, pool: Any, info: dict) -> None:
        self.client, self.pool = client, pool
        self.info = info
        self.reply_context: ContextVar[ReplyContext | None] = ContextVar(f'{self.spec.name}_reply_context', default=None)

    @property
    def platform(self) -> str:
        """Имя платформы (общие обработчики различают платформы по этому атрибуту)."""
        return self.spec.name

    # --- что реализует платформа ---------------------------------------------------------------------------------
    def sender(self, connection: Any) -> Any:
        """Отправитель сообщений платформы для указанного соединения."""
        raise NotImplementedError

    async def get_me(self) -> SimpleNamespace:
        """Данные самого бота (id и имя пользователя)."""
        raise NotImplementedError

    def split_text(self, part: str) -> list[dict]:
        """Части очереди доставки для обычного текста платформы."""
        parts = self.spec.split(part, self.spec.text_limit) if self.spec.split else [part]
        return [dict(kind=self.spec.chunk_kind, text=item) if isinstance(item, str) else item for item in parts]

    def attach_keyboard(self, chunk: dict, markup: Any, chat: Any) -> None:
        """Добавить в часть очереди кнопки, преобразованные для платформы."""
        raise NotImplementedError

    def welcome_chunk(self, text: Any, digest: str, markup: Any) -> dict:
        """Часть приветствия с картинкой и постоянной клавиатурой."""
        raise NotImplementedError

    # --- общая часть ---------------------------------------------------------------------------------------------
    def _key(self) -> str:
        """Выдать следующий ключ идемпотентности ответа текущего события; вне контекста события — ошибка."""
        context = self.reply_context.get()
        if context is None:
            raise RuntimeError(f'{self.spec.title} response requires a durable event context')
        key = f'{self.spec.name}-event:{context.key}:reply:{context.position}'
        context.position += 1
        return key

    async def queue_response(self, connection: Any, settings: Any, chat_id: int, text: Any, markup: Any = None,
                             *, frozen_chunks: list | None = None) -> int:
        """Сохранить готовый ответ интерфейса в очередь доставки до того, как событие будет помечено обработанным: текст режется на части
        платформы, добавляются карточки языков и кнопки."""
        key = self._key()
        chat = await connection.fetchrow('SELECT * FROM telegram_chats WHERE telegram_chat_id=$1 AND platform=$2', chat_id, self.spec.name)
        if not chat:
            raise UserError('forbidden')
        if frozen_chunks is None:
            chunks = illustrations.chunks(text, getattr(text, 'image_id', None), min(self.spec.text_limit, settings.max_message_length))
            chunks = await message_languages.prepare(connection, text, chunks, chat, source_key=key)
            prepared: list = []
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get('card_id'):
                    prepared.append(chunk)
                elif isinstance(chunk, str):
                    prepared.extend(self.split_text(chunk))
                else:
                    prepared.append(chunk)
            frozen_chunks = prepared
            if markup is not None and frozen_chunks and not frozen_chunks[-1].get('card_id'):
                self.attach_keyboard(frozen_chunks[-1], markup, chat)
        translation = await bible.chat_translation(connection, chat_id)
        mode = f'{self.spec.name}_ui'
        return await insert_payload(connection, chat, translation or {'id': None}, str(text), key, mode, {'kind': mode},
                                    frozen_chunks=frozen_chunks)

    async def queue_welcome(self, connection: Any, settings: Any, chat_id: int, text: Any, markup: Any) -> Any:
        """Поставить в очередь приветствие с картинкой (хеш изображения защищает от подмены) и клавиатурой."""
        import hashlib

        from app.bot.ui import WELCOME_PATH
        digest = hashlib.sha256(WELCOME_PATH.read_bytes()).hexdigest()
        return await self.queue_response(connection, settings, chat_id, text, markup,
                                         frozen_chunks=[self.welcome_chunk(text, digest, markup)])
