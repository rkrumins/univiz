"""Unit tests for the Graph Store outbox relay.

DB-independent: the Graph Store models use Postgres ``JSONB`` (a
plan-approved deviation) so they cannot run on the test-suite's SQLite.
We test the relay *contract* — claim → publish → mark processed, batch
handling, empty poll, publish-failure leaves rows unprocessed — with a
mocked session, which is exactly the behaviour the at-least-once
delivery guarantee depends on.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.graph_outbox_relay import drain_once


def _event(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"evt_{i}",
        event_type="visualization.graph.committed",
        event_version=1,
        aggregate_type="graph",
        aggregate_id=f"g_{i}",
        payload={"i": i},
        processed=False,
    )


def _session_returning(rows):
    """A fake AsyncSession whose execute() yields `rows` via
    .scalars().all() — mirrors the SELECT ... FOR UPDATE SKIP LOCKED."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_drain_publishes_then_marks_processed():
    rows = [_event(0), _event(1), _event(2)]
    published = []

    async def publish(ev):
        # Contract: must NOT be marked processed before publish returns.
        assert ev.processed is False
        published.append(ev.id)

    n = await drain_once(_session_returning(rows), publish, batch_size=100)

    assert n == 3
    assert published == ["evt_0", "evt_1", "evt_2"]
    assert all(r.processed is True for r in rows)


@pytest.mark.asyncio
async def test_drain_empty_is_noop():
    published = []
    n = await drain_once(_session_returning([]), lambda e: published.append(e))
    assert n == 0
    assert published == []


@pytest.mark.asyncio
async def test_publish_failure_aborts_batch_unprocessed():
    rows = [_event(0), _event(1), _event(2)]

    async def publish(ev):
        if ev.id == "evt_1":
            raise RuntimeError("redis down")

    with pytest.raises(RuntimeError, match="redis down"):
        await drain_once(_session_returning(rows), publish, batch_size=100)

    # evt_0 published+marked, evt_1 failed mid-publish, evt_2 untouched.
    # The caller's session scope rolls the whole transaction back, so
    # NONE of these processed=True values are ever committed — the
    # batch is retried wholesale next poll (at-least-once).
    assert rows[0].processed is True   # set in-memory, but rolled back by scope
    assert rows[1].processed is False
    assert rows[2].processed is False


@pytest.mark.asyncio
async def test_batch_size_forwarded_to_query_limit():
    session = _session_returning([])
    await drain_once(session, AsyncMock(), batch_size=7)
    # The compiled statement should carry our limit; assert via the
    # statement object passed to execute().
    stmt = session.execute.call_args[0][0]
    from sqlalchemy.dialects import postgresql

    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LIMIT 7" in compiled
    assert "FOR UPDATE SKIP LOCKED" in compiled
