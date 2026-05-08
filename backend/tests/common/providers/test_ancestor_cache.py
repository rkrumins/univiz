"""Unit tests for AncestorChainCache.

Uses an in-process Redis fake (a dict-backed object satisfying the
protocol) so tests do not require a running Redis. The fake also
exposes a switch to fail every operation, which exercises the
graceful-degradation path.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

import pytest

from backend.common.providers.ancestor_cache import AncestorChainCache


class _FakeRedis:
    """Async fake satisfying the protocol AncestorChainCache uses."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.fail_mode: Optional[str] = None  # "always" | None
        self.calls: List[str] = []

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_mode == "always":
            raise RuntimeError(f"fake redis injected failure: {op}")

    async def hget(self, name: str, key: str):
        self._maybe_fail("hget")
        h = self.hashes.get(name, {})
        v = h.get(key)
        return v.encode("utf-8") if isinstance(v, str) else v

    async def hmget(self, name: str, keys):
        self._maybe_fail("hmget")
        h = self.hashes.get(name, {})
        return [h.get(k).encode("utf-8") if k in h else None for k in keys]

    async def hset(self, name: str, key: str, value: str):
        self._maybe_fail("hset")
        self.hashes.setdefault(name, {})[key] = value
        return 1

    async def hmset(self, name: str, mapping):
        self._maybe_fail("hmset")
        self.hashes.setdefault(name, {}).update(mapping)
        return True

    async def hdel(self, name: str, *keys):
        self._maybe_fail("hdel")
        h = self.hashes.get(name, {})
        for k in keys:
            h.pop(k, None)
        return len(keys)

    async def delete(self, *names):
        self._maybe_fail("delete")
        n = 0
        for name in names:
            if name in self.hashes:
                del self.hashes[name]
                n += 1
        return n

    async def expire(self, name: str, time_s: int):
        self._maybe_fail("expire")
        return name in self.hashes


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_then_get_round_trips_via_redis():
    redis = _FakeRedis()
    cache = AncestorChainCache(namespace="g1", redis_client=redis)

    await cache.set("urn:a", ["urn:root", "urn:p"], fingerprint="fp1")
    chain = await cache.get("urn:a", fingerprint="fp1")
    assert chain == ["urn:root", "urn:p"]


@pytest.mark.asyncio
async def test_set_then_get_round_trips_via_memory_only():
    cache = AncestorChainCache(namespace="g1", redis_client=None)

    await cache.set("urn:a", ["urn:root"], fingerprint="fp1")
    chain = await cache.get("urn:a", fingerprint="fp1")
    assert chain == ["urn:root"]


@pytest.mark.asyncio
async def test_get_returns_none_on_miss():
    cache = AncestorChainCache(namespace="g1", redis_client=None)
    assert await cache.get("urn:absent", fingerprint="fp1") is None


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mget_returns_only_hits():
    redis = _FakeRedis()
    cache = AncestorChainCache(namespace="g1", redis_client=redis)

    await cache.set("urn:a", ["urn:root"], fingerprint="fp1")
    await cache.set("urn:b", ["urn:root", "urn:b_parent"], fingerprint="fp1")

    out = await cache.mget(["urn:a", "urn:b", "urn:c"], fingerprint="fp1")
    assert out == {
        "urn:a": ["urn:root"],
        "urn:b": ["urn:root", "urn:b_parent"],
    }
    assert "urn:c" not in out


@pytest.mark.asyncio
async def test_mset_writes_all():
    cache = AncestorChainCache(namespace="g1", redis_client=None)
    await cache.mset(
        {"urn:a": ["urn:r"], "urn:b": ["urn:r", "urn:bp"]},
        fingerprint="fp1",
    )
    assert await cache.get("urn:a", fingerprint="fp1") == ["urn:r"]
    assert await cache.get("urn:b", fingerprint="fp1") == ["urn:r", "urn:bp"]


# ---------------------------------------------------------------------------
# Fingerprint isolates namespaces
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_fingerprints_do_not_collide():
    cache = AncestorChainCache(namespace="g1", redis_client=None)

    await cache.set("urn:a", ["chain_v1"], fingerprint="fp_v1")
    await cache.set("urn:a", ["chain_v2"], fingerprint="fp_v2")

    # Each fingerprint preserves its own chain.
    assert await cache.get("urn:a", fingerprint="fp_v1") == ["chain_v1"]
    assert await cache.get("urn:a", fingerprint="fp_v2") == ["chain_v2"]


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate_drops_one_urn():
    cache = AncestorChainCache(namespace="g1", redis_client=None)
    await cache.set("urn:a", ["x"], fingerprint="fp1")
    await cache.set("urn:b", ["y"], fingerprint="fp1")

    await cache.invalidate("urn:a", fingerprint="fp1")

    assert await cache.get("urn:a", fingerprint="fp1") is None
    assert await cache.get("urn:b", fingerprint="fp1") == ["y"]


@pytest.mark.asyncio
async def test_invalidate_fingerprint_drops_namespace():
    redis = _FakeRedis()
    cache = AncestorChainCache(namespace="g1", redis_client=redis)
    await cache.set("urn:a", ["x"], fingerprint="fp1")
    await cache.set("urn:b", ["y"], fingerprint="fp1")
    await cache.set("urn:a", ["x2"], fingerprint="fp2")

    await cache.invalidate_fingerprint(fingerprint="fp1")

    assert await cache.get("urn:a", fingerprint="fp1") is None
    assert await cache.get("urn:b", fingerprint="fp1") is None
    # Other fingerprint untouched.
    assert await cache.get("urn:a", fingerprint="fp2") == ["x2"]


# ---------------------------------------------------------------------------
# In-memory TTL & LRU
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_memory_ttl_eviction(monkeypatch):
    cache = AncestorChainCache(
        namespace="g1", redis_client=None,
        in_memory_capacity=10, ttl_s=1,
    )
    await cache.set("urn:a", ["x"], fingerprint="fp")

    # Advance the monotonic clock past the TTL.
    fake_now = [time.monotonic() + 5.0]
    monkeypatch.setattr(
        "backend.common.providers.ancestor_cache.time.monotonic",
        lambda: fake_now[0],
    )
    assert await cache.get("urn:a", fingerprint="fp") is None


@pytest.mark.asyncio
async def test_in_memory_lru_eviction():
    cache = AncestorChainCache(
        namespace="g1", redis_client=None,
        in_memory_capacity=2, ttl_s=3600,
    )
    await cache.set("urn:a", ["a"], fingerprint="fp")
    await cache.set("urn:b", ["b"], fingerprint="fp")
    # Touch a so b becomes LRU.
    await cache.get("urn:a", fingerprint="fp")
    await cache.set("urn:c", ["c"], fingerprint="fp")

    assert await cache.get("urn:b", fingerprint="fp") is None
    assert await cache.get("urn:a", fingerprint="fp") == ["a"]
    assert await cache.get("urn:c", fingerprint="fp") == ["c"]


# ---------------------------------------------------------------------------
# Redis failures fall back to memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_memory():
    redis = _FakeRedis()
    cache = AncestorChainCache(namespace="g1", redis_client=redis)

    # First write: succeeds on both layers.
    await cache.set("urn:a", ["x"], fingerprint="fp")

    # Now break Redis. Reads should still return from memory.
    redis.fail_mode = "always"
    assert await cache.get("urn:a", fingerprint="fp") == ["x"]


@pytest.mark.asyncio
async def test_redis_disabled_after_failure_streak():
    redis = _FakeRedis()
    cache = AncestorChainCache(namespace="g1", redis_client=redis)
    redis.fail_mode = "always"

    # Drive Redis past the failure threshold.
    for _ in range(6):
        await cache.set("urn:a", ["x"], fingerprint="fp")

    # Subsequent reads should not even attempt Redis (no new entries in calls).
    redis.calls.clear()
    await cache.get("urn:a", fingerprint="fp")
    assert "hget" not in redis.calls
