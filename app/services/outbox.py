"""Transport-independent, checkpointed delivery state machine.

A timeout is *not* evidence of failed delivery. Ambiguous outcomes require a human
choice rather than automatically sending the same message again.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from app.services.errors import SendError


@dataclass(frozen=True, slots=True)
class Envelope:
    """An immutable payload with the next unconfirmed message position."""
    id: int
    chat_id: int
    chunks: tuple[str, ...]
    next_chunk: int = 0
    thread_id: int | None = None


class Checkpoints(Protocol):
    """Persistence operations must fail loudly; errors must never mean success."""
    async def begin(self, envelope: Envelope) -> bool: ...
    async def acknowledge(self, envelope: Envelope, message_id: int) -> None: ...
    async def fail(self, envelope: Envelope, kind: str, retry_after: float) -> None: ...


class Sender(Protocol):
    """Known API rejections are SendError; network ambiguity is 'uncertain'."""
    async def send(self, chat_id: int, text: str, thread_id: int | None) -> int: ...


async def dispatch_chunk(envelope: Envelope, checkpoints: Checkpoints, sender: Sender) -> str:
    """Send exactly one chunk and commit the acknowledgement before advancing.

    Storage failures, including after a successful API response, propagate. The
    persistent 'sending' checkpoint is reviewed after exclusive-worker restart.
    CancelledError also propagates, preserving that same safety property.
    """
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
