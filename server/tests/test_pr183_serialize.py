"""PR-183 S1 — InlineQueue serialises jobs so concurrent analyses don't
collide on SQLite's single-writer lock.
"""

import asyncio

import pytest

from app.local_mode import InlineQueue


@pytest.mark.asyncio
async def test_inline_queue_serializes_jobs(monkeypatch):
    q = InlineQueue()
    events: list[tuple[str, str]] = []

    async def job(ctx, tag):
        events.append(("start", tag))
        await asyncio.sleep(0.05)
        events.append(("end", tag))

    monkeypatch.setattr(q, "_registry", lambda: {"job": job})

    await q.enqueue_job("job", "A")
    await q.enqueue_job("job", "B")
    tasks = list(q._tasks)
    assert len(tasks) == 2  # both enqueued concurrently
    await asyncio.gather(*tasks)

    # Serialised: each job runs to completion before the next starts — no
    # interleave (the bug would be start,start,end,end). Order of A/B is not
    # asserted, only that they don't overlap.
    assert [e[0] for e in events] == ["start", "end", "start", "end"]
    assert events[0][1] == events[1][1]
    assert events[2][1] == events[3][1]
    assert events[0][1] != events[2][1]
