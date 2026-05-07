"""Tests for the per-job ancestors-cache lifecycle on the FalkorDB provider.

The cache (`{graph_name}:ancestors` Redis Hash) is intra-job memoization:
populated lazily during a single aggregation run, wiped at the start of
the next. These tests exercise the wipe path (``reset_ancestors_cache``)
and the change-detected fallback in ``set_containment_edge_types``
without spinning up Redis or FalkorDB.
"""
import asyncio

import pytest

from backend.app.providers.falkordb_provider import FalkorDBProvider


class _FakeRedis:
    """Records ``delete`` calls. Optional ``raise_on_delete`` simulates
    a Redis outage to confirm the wipe path is best-effort."""

    def __init__(self, *, raise_on_delete: bool = False):
        self.deleted_keys: list[str] = []
        self.raise_on_delete = raise_on_delete

    async def delete(self, *keys):
        if self.raise_on_delete:
            raise RuntimeError("simulated redis outage")
        self.deleted_keys.extend(keys)
        return len(keys)


def _make_provider(redis: _FakeRedis | None = None, graph_name: str = "demo_graph") -> FalkorDBProvider:
    """Build a provider shell sufficient to exercise the cache helpers
    without touching a real graph or Redis. ``__init__`` is bypassed so
    we don't need provider config / connection state."""
    p = FalkorDBProvider.__new__(FalkorDBProvider)
    p._redis = redis if redis is not None else _FakeRedis()  # type: ignore[attr-defined]
    p._graph_name = graph_name  # type: ignore[attr-defined]
    return p


def test_reset_ancestors_cache_calls_redis_delete():
    redis = _FakeRedis()
    provider = _make_provider(redis, graph_name="g1")
    asyncio.run(provider.reset_ancestors_cache())
    assert redis.deleted_keys == ["g1:ancestors"]


def test_reset_ancestors_cache_swallows_redis_errors():
    redis = _FakeRedis(raise_on_delete=True)
    provider = _make_provider(redis)
    # Must not raise — the method is best-effort.
    asyncio.run(provider.reset_ancestors_cache())


def test_set_containment_no_op_when_unchanged_does_not_invalidate():
    redis = _FakeRedis()
    provider = _make_provider(redis)

    async def run():
        provider.set_containment_edge_types(["CONTAINS"], from_ontology=True)
        # Drain any pending tasks.
        await asyncio.sleep(0)
        provider.set_containment_edge_types(["contains"], from_ontology=True)
        await asyncio.sleep(0)

    asyncio.run(run())
    # The first call is the initial set (no prior state, no
    # invalidation). The second call's normalized type set
    # ({"CONTAINS"}) equals the first's, so still no invalidation.
    assert redis.deleted_keys == []


def test_set_containment_change_triggers_async_invalidation():
    redis = _FakeRedis()
    provider = _make_provider(redis)

    async def run():
        # First set establishes baseline — no invalidation fires.
        provider.set_containment_edge_types(["CONTAINS"], from_ontology=True)
        await asyncio.sleep(0)
        assert redis.deleted_keys == []

        # Genuinely different type set — invalidation must fire.
        provider.set_containment_edge_types(["CONTAINS", "HAS_COLUMN"], from_ontology=True)
        # Allow the scheduled task to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert redis.deleted_keys == ["demo_graph:ancestors"]


def test_set_containment_outside_event_loop_does_not_raise():
    """``set_containment_edge_types`` is sync. When called from a
    sync context (test bootstrap, ContextEngine in non-async paths)
    there is no running loop to schedule on; the change-detected
    branch must skip silently rather than raise. The worker's
    explicit ``reset_ancestors_cache`` call covers the production
    path."""
    redis = _FakeRedis()
    provider = _make_provider(redis)

    # First call: baseline.
    provider.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    # Second call (different): change-detection runs but no loop —
    # must not raise, must not enqueue a task.
    provider.set_containment_edge_types(["CONTAINS", "HAS_COLUMN"], from_ontology=True)
    assert redis.deleted_keys == []
