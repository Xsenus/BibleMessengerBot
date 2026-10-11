"""Конечный автомат доставки с контрольными точками, не зависящий от транспорта.

Тайм-аут не доказывает, что сообщение не доставлено: неоднозначный результат требует решения человека, а не автоматической повторной отправки.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from app.services.errors import SendError


@dataclass(frozen=True, slots=True)
class Envelope:
    """Неизменяемое задание: ID, чат, части сообщения, позиция следующей неподтверждённой части и тема форума."""
    id: int
    chat_id: int
    chunks: tuple[str | dict, ...]
    next_chunk: int = 0
    thread_id: int | None = None


class Checkpoints(Protocol):
    """Контракт хранения контрольных точек: `begin` (пометить начало отправки), `acknowledge` (записать подтверждение), `fail` (записать сбой). Ошибки хранилища должны быть громкими и никогда не означать успех."""
    async def begin(self, envelope: Envelope) -> bool: ...
    async def acknowledge(self, envelope: Envelope, message_id: int) -> None: ...
    async def fail(self, envelope: Envelope, kind: str, retry_after: float) -> None: ...


class Sender(Protocol):
    """Контракт отправителя: известные отказы API — SendError, сетевая неопределённость — `uncertain`."""
    async def send(self, chat_id: int, text: str | dict, thread_id: int | None) -> int: ...


async def dispatch_chunk(envelope: Envelope, checkpoints: Checkpoints, sender: Sender) -> str:
    """Отправить ровно одну часть и зафиксировать подтверждение до перехода к следующей. Возвращает `stale`, `sent`, `partial` или вид ошибки. Сбой хранилища (в том числе после успешного ответа API) поднимается наружу: контрольная точка `sending` будет разобрана после перезапуска."""
    if not envelope.chunks or not 0 <= envelope.next_chunk < len(envelope.chunks):
        raise ValueError('Invalid delivery checkpoint')
    if not await checkpoints.begin(envelope):
        return 'stale'
    try:
        message_id = await sender.send(envelope.chat_id,
            envelope.chunks[envelope.next_chunk],envelope.thread_id)
    except SendError as error:
        await checkpoints.fail(envelope,error.kind,error.retry_after)
        return error.kind
    except Exception:
        # Unknown transport exceptions have the same uncertainty as a timeout.
        await checkpoints.fail(envelope,'uncertain',0)
        return 'uncertain'
    if not isinstance(message_id,int) or isinstance(message_id,bool) or message_id <= 0:
        await checkpoints.fail(envelope,'uncertain',0)
        return 'uncertain'
    await checkpoints.acknowledge(envelope,message_id)
    return 'sent' if envelope.next_chunk + 1 == len(envelope.chunks) else 'partial'
