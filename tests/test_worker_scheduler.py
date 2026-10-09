"""Concurrent fake I/O tests: no Telegram or paid provider requests."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.worker import scheduler


class Pool:
    @asynccontextmanager
    async def acquire(self):
        yield SimpleNamespace(execute=AsyncMock())


@pytest.mark.asyncio
async def test_slow_chat_does_not_block_80_other_chats_and_slots_are_bounded(monkeypatch):
    pending = [dict(id=i, telegram_chat_id=i) for i in range(1, 82)]
    started, completed, inflight = set(), set(), set()
    slow_started, slow_release, others_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()
    maximum = 0

    async def jobs(_connection, busy, limit):
        selected = []
        for job in pending:
            chat = job['telegram_chat_id']
            if chat not in completed and chat not in busy:
                selected.append(job)
                if len(selected) == limit:
                    break
        return selected

    async def process(_connection, identifier, _sender):
        nonlocal maximum
        assert identifier not in started
        started.add(identifier)
        inflight.add(identifier)
        maximum = max(maximum, len(inflight))
        if identifier == 1:
            slow_started.set()
            await slow_release.wait()
        else:
            await asyncio.sleep(0)
        inflight.remove(identifier)
        completed.add(identifier)
        if len(completed-{1}) == 80:
            others_done.set()
        return 'sent'

    monkeypatch.setattr(scheduler, 'due_jobs', jobs)
    monkeypatch.setattr(scheduler, 'process_delivery', process)
    task = asyncio.create_task(scheduler.send_loop(Pool(), lambda _:None, stop, poll=0.001))
    try:
        await asyncio.wait_for(slow_started.wait(), 2)
        await asyncio.wait_for(others_done.wait(), 2)
        assert 1 not in completed and maximum == 4
        stop.set()
        await asyncio.sleep(0.01)
        assert not task.done()  # A graceful stop waits for the outstanding acknowledgement.
        slow_release.set()
        await asyncio.wait_for(task, 2)
        assert len(completed) == 81
    finally:
        slow_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_preparation_does_not_block_delivery(monkeypatch):
    preparing, release, delivered, stop = (asyncio.Event() for _ in range(4))

    async def prepare(_connection, _settings):
        preparing.set()
        await release.wait()

    async def jobs(_connection, busy, _limit):
        return [] if busy or delivered.is_set() else [dict(id=1, telegram_chat_id=1)]

    async def process(*_):
        delivered.set()
        return 'sent'

    monkeypatch.setattr(scheduler, 'prepare_once', prepare)
    monkeypatch.setattr(scheduler, 'due_jobs', jobs)
    monkeypatch.setattr(scheduler, 'process_delivery', process)
    prep = asyncio.create_task(scheduler.prepare_loop(Pool(), SimpleNamespace()))
    send = asyncio.create_task(scheduler.send_loop(Pool(), lambda _:None, stop, poll=0.001))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        await asyncio.wait_for(delivered.wait(), 2)
        assert not release.is_set()
        stop.set()
        await asyncio.wait_for(send, 2)
    finally:
        prep.cancel()
        send.cancel()
        await asyncio.gather(prep, send, return_exceptions=True)


@pytest.mark.asyncio
async def test_database_failure_is_propagated_instead_of_lost_in_background(monkeypatch):
    async def jobs(_connection, busy, _limit):
        return [] if busy else [dict(id=1, telegram_chat_id=1)]

    monkeypatch.setattr(scheduler, 'due_jobs', jobs)
    monkeypatch.setattr(scheduler, 'process_delivery', AsyncMock(side_effect=RuntimeError('database lost')))
    with pytest.raises(RuntimeError, match='database lost'):
        await asyncio.wait_for(scheduler.send_loop(Pool(), lambda _:None, asyncio.Event(), poll=0.001), 2)
