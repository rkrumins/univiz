"""Unit tests for :mod:`backend.app.services.graph_cache`.

Mocks the async Redis client directly — fakeredis is not part of the
test toolchain and the cache only exercises `GET`, `SET`, and `INCR`,
which are trivial to mock.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel
from redis.exceptions import RedisError

from backend.app.services import graph_cache
from backend.app.services.graph_cache import (
    CacheScope,
    ENDPOINT_AGGREGATED,
    ENDPOINT_CHILDREN,
    GraphCache,
    _build_key,
)


class _Result(BaseModel):
    """Minimal Pydantic model standing in for ChildrenWithEdgesResult."""
    value: int
    children: list = []


def _make_redis() -> AsyncMock:
    """An AsyncMock with the surface graph_cache touches: get/set/incr."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    return redis


@pytest.fixture(autouse=True)
def _enable_children_endpoint(monkeypatch):
    """Force the children-with-edges flag on for tests that exercise it."""
    monkeypatch.setitem(
        graph_cache._ENABLED_ENDPOINTS, ENDPOINT_CHILDREN, True,
    )
    monkeypatch.setitem(
        graph_cache._ENABLED_ENDPOINTS, ENDPOINT_AGGREGATED, True,
    )


# ─── basic hit / miss ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_miss_calls_compute_and_caches() -> None:
    redis = _make_redis()
    cache = GraphCache(redis)
    compute = AsyncMock(return_value=_Result(value=42))

    result = await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={"urn": "a"},
        compute=compute,
        model_cls=_Result,
    )

    assert result.value == 42
    compute.assert_awaited_once()
    redis.set.assert_awaited_once()
    # The value was serialized as JSON
    set_args = redis.set.call_args
    payload = set_args.args[1] if len(set_args.args) > 1 else set_args.kwargs.get("value")
    assert "42" in payload


@pytest.mark.asyncio
async def test_hit_returns_cached_without_compute() -> None:
    redis = _make_redis()
    redis.get = AsyncMock(side_effect=[
        "0",  # generation read
        _Result(value=99).model_dump_json(by_alias=True),  # cached payload
    ])
    cache = GraphCache(redis)
    compute = AsyncMock(side_effect=AssertionError("compute should not run"))

    result = await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={"urn": "a"},
        compute=compute,
        model_cls=_Result,
    )

    assert result.value == 99
    compute.assert_not_called()


@pytest.mark.asyncio
async def test_feature_flag_off_bypasses_cache(monkeypatch) -> None:
    monkeypatch.setitem(
        graph_cache._ENABLED_ENDPOINTS, ENDPOINT_CHILDREN, False,
    )
    redis = _make_redis()
    cache = GraphCache(redis)
    compute = AsyncMock(return_value=_Result(value=1))

    await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={},
        compute=compute,
        model_cls=_Result,
    )

    compute.assert_awaited_once()
    redis.get.assert_not_called()
    redis.set.assert_not_called()


# ─── singleflight ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_in_process_singleflight_coalesces_concurrent_calls() -> None:
    redis = _make_redis()
    cache = GraphCache(redis)

    call_count = 0
    gate = asyncio.Event()

    async def slow_compute() -> _Result:
        nonlocal call_count
        call_count += 1
        # Block until the second caller has had time to coalesce on the future
        await gate.wait()
        return _Result(value=7)

    async def trigger() -> _Result:
        return await cache.get_or_compute(
            scope=CacheScope("ws1", "ds1"),
            endpoint=ENDPOINT_CHILDREN,
            params={"urn": "shared"},
            compute=slow_compute,
            model_cls=_Result,
        )

    task_a = asyncio.create_task(trigger())
    task_b = asyncio.create_task(trigger())
    # Let both tasks reach the in-flight registration before unblocking.
    await asyncio.sleep(0.01)
    gate.set()
    result_a, result_b = await asyncio.gather(task_a, task_b)

    assert result_a.value == 7
    assert result_b.value == 7
    assert call_count == 1


# ─── invalidation via generation bump ──────────────────────────────────

@pytest.mark.asyncio
async def test_bump_generation_changes_cache_key() -> None:
    scope = CacheScope("ws1", "ds1")
    params = {"urn": "x"}
    key_g0 = _build_key(scope, 0, ENDPOINT_CHILDREN, params)
    key_g1 = _build_key(scope, 1, ENDPOINT_CHILDREN, params)
    assert key_g0 != key_g1
    assert ":0:" in key_g0
    assert ":1:" in key_g1


@pytest.mark.asyncio
async def test_bump_generation_issues_incr() -> None:
    redis = _make_redis()
    cache = GraphCache(redis)
    await cache.bump_generation(CacheScope("ws1", "ds1"))
    redis.incr.assert_awaited_once()
    key_arg = redis.incr.call_args.args[0]
    assert "ws1" in key_arg
    assert "ds1" in key_arg


# ─── fail-open semantics ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redis_get_failure_falls_through_to_compute() -> None:
    redis = _make_redis()
    redis.get = AsyncMock(side_effect=RedisError("connection refused"))
    cache = GraphCache(redis)
    compute = AsyncMock(return_value=_Result(value=5))

    result = await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={},
        compute=compute,
        model_cls=_Result,
    )

    assert result.value == 5
    compute.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_set_failure_does_not_fail_request() -> None:
    redis = _make_redis()
    redis.set = AsyncMock(side_effect=RedisError("write failed"))
    cache = GraphCache(redis)
    compute = AsyncMock(return_value=_Result(value=8))

    result = await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={},
        compute=compute,
        model_cls=_Result,
    )
    assert result.value == 8


# ─── empty-result short TTL ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_result_caches_with_negative_ttl() -> None:
    redis = _make_redis()
    cache = GraphCache(redis)
    compute = AsyncMock(return_value=_Result(value=0, children=[]))

    await cache.get_or_compute(
        scope=CacheScope("ws1", "ds1"),
        endpoint=ENDPOINT_CHILDREN,
        params={},
        compute=compute,
        model_cls=_Result,
    )

    set_kwargs = redis.set.call_args.kwargs
    # ex (expiry in seconds) should equal the negative-cache value
    assert set_kwargs["ex"] == graph_cache._NEGATIVE_TTL


# ─── key stability ─────────────────────────────────────────────────────

def test_params_order_does_not_affect_key() -> None:
    scope = CacheScope("ws1", "ds1")
    k1 = _build_key(scope, 0, ENDPOINT_CHILDREN, {"a": 1, "b": 2})
    k2 = _build_key(scope, 0, ENDPOINT_CHILDREN, {"b": 2, "a": 1})
    assert k1 == k2


def test_different_scopes_yield_different_keys() -> None:
    k1 = _build_key(CacheScope("ws1", "ds1"), 0, ENDPOINT_CHILDREN, {})
    k2 = _build_key(CacheScope("ws2", "ds1"), 0, ENDPOINT_CHILDREN, {})
    k3 = _build_key(CacheScope("ws1", "ds2"), 0, ENDPOINT_CHILDREN, {})
    assert len({k1, k2, k3}) == 3
