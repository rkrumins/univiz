"""Ancestor chain cache used by all graph providers.

A node's "ancestor chain" is the ordered list of containment ancestors
above it (e.g. Dataset -> Schema -> Database -> Domain). It is read on
the hot path of AGGREGATED-edge materialisation and Trace v2 anchor
hydration. Recomputing it from the graph on every read is the dominant
cost driver during ingestion sweeps.

Design points
-------------

* The cache key is a function of (namespace, fingerprint, urn) where
  ``fingerprint`` is a stable digest of the resolved containment edge
  types. A change in the ontology routes reads/writes into a different
  key space without explicit invalidation -- stale entries simply age
  out of the old namespace.

* When a Redis client is available the canonical store is a Redis Hash
  (one hash per namespace+fingerprint). Hashes give us O(1) HGET and
  HMGET for batch reads, which the materialiser uses to fetch chains
  for hundreds of URNs in one round-trip.

* When Redis is not available we fall back to a small in-process
  TTL+LRU. Behaviour is identical from the caller's perspective; the
  fallback is intended for dev/test and for graceful degradation when
  Redis is briefly unreachable.

* Idempotent `set` writes overwrite. The `invalidate` API supports
  per-URN HDEL (called from ``on_containment_changed``) and a full
  namespace flush (called when the provider intentionally resets).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis backend protocol
# ---------------------------------------------------------------------------

class _RedisLike(Protocol):
    """Minimal subset of redis.asyncio.Redis we depend on.

    Spelt as a Protocol so the tests can pass a fake without importing
    redis. Only the calls we actually use are typed; the real client
    has many more methods.
    """

    async def hget(self, name: str, key: str) -> Optional[bytes]: ...
    async def hmget(self, name: str, keys: List[str]) -> List[Optional[bytes]]: ...
    async def hset(self, name: str, key: str, value: str) -> int: ...
    async def hmset(self, name: str, mapping: Dict[str, str]) -> bool: ...
    async def hdel(self, name: str, *keys: str) -> int: ...
    async def delete(self, *names: str) -> int: ...
    async def expire(self, name: str, time_s: int) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory fallback (TTL + LRU)
# ---------------------------------------------------------------------------

class _InMemoryStore:
    """Tiny TTL+LRU; intentionally not thread-safe (asyncio single-thread).

    Stores (chain, expires_at_monotonic) tuples keyed by the same
    ``{namespace}:{fingerprint}:{urn}`` string the Redis path uses.
    """

    __slots__ = ("_capacity", "_ttl_s", "_data")

    def __init__(self, capacity: int, ttl_s: int) -> None:
        self._capacity = capacity
        self._ttl_s = ttl_s
        self._data: "OrderedDict[str, tuple[List[str], float]]" = OrderedDict()

    def get(self, key: str) -> Optional[List[str]]:
        entry = self._data.get(key)
        if entry is None:
            return None
        chain, expires_at = entry
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        # LRU touch
        self._data.move_to_end(key)
        return chain

    def set(self, key: str, chain: List[str]) -> None:
        expires_at = time.monotonic() + self._ttl_s
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (chain, expires_at)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            self._data.pop(k, None)
        return len(keys)


# ---------------------------------------------------------------------------
# Public cache
# ---------------------------------------------------------------------------

class AncestorChainCache:
    """Provider-agnostic ancestor-chain cache.

    Construction
    ------------
    ``namespace`` is typically the graph identifier (FalkorDB graph name,
    Neo4j database name, Spanner database+graph). It scopes the keyspace
    so multi-tenant deployments do not cross-pollute.

    ``redis_client`` is optional. When None or when Redis is reachable but
    fails on a write, the cache transparently falls back to the in-memory
    LRU. A subsequent successful Redis call rehydrates the in-memory
    layer on read; we do not actively reconcile.

    Reads
    -----
    ``get`` returns ``None`` on a miss. ``mget`` returns a dict that omits
    miss keys -- callers iterate the input list and treat absent entries
    as misses.
    """

    def __init__(
        self,
        *,
        namespace: str,
        redis_client: Optional[_RedisLike] = None,
        in_memory_capacity: int = 50_000,
        ttl_s: int = 3600,
    ) -> None:
        self._namespace = namespace
        self._redis = redis_client
        self._memory = _InMemoryStore(in_memory_capacity, ttl_s)
        self._ttl_s = ttl_s
        # Track Redis health for graceful degradation. A single failed
        # write does not mark Redis dead; a configurable streak does.
        self._redis_failures = 0
        self._redis_failure_threshold = 5
        self._lock = asyncio.Lock()

    # ----- Key shape -------------------------------------------------------

    def _hash_key(self, fingerprint: str) -> str:
        """Redis Hash key for one (namespace, fingerprint) pair.

        All ancestor chains computed under the same ontology share the
        hash so HMGET can fetch hundreds in one round-trip. Distinct
        fingerprints land in distinct hashes.
        """
        return f"{self._namespace}:ancestors:{fingerprint}"

    def _memory_key(self, fingerprint: str, urn: str) -> str:
        return f"{self._namespace}:ancestors:{fingerprint}:{urn}"

    def _memory_prefix(self, fingerprint: str) -> str:
        return f"{self._namespace}:ancestors:{fingerprint}:"

    # ----- Read API --------------------------------------------------------

    async def get(self, urn: str, *, fingerprint: str) -> Optional[List[str]]:
        """Return the cached chain for ``urn`` or None on miss."""
        if self._redis_alive():
            try:
                raw = await self._redis.hget(self._hash_key(fingerprint), urn)
                if raw is not None:
                    chain = self._decode(raw)
                    # Warm in-memory layer for subsequent fast paths.
                    self._memory.set(self._memory_key(fingerprint, urn), chain)
                    self._note_redis_ok()
                    return chain
            except Exception as exc:
                self._note_redis_fail(exc, op="hget")
        return self._memory.get(self._memory_key(fingerprint, urn))

    async def mget(
        self,
        urns: List[str],
        *,
        fingerprint: str,
    ) -> Dict[str, List[str]]:
        """Batch read. Returns only hits; absent URNs are simply missing."""
        if not urns:
            return {}
        out: Dict[str, List[str]] = {}
        missing_after_redis: List[str] = []
        if self._redis_alive():
            try:
                raw_list = await self._redis.hmget(self._hash_key(fingerprint), urns)
                self._note_redis_ok()
                for urn, raw in zip(urns, raw_list):
                    if raw is None:
                        missing_after_redis.append(urn)
                        continue
                    chain = self._decode(raw)
                    out[urn] = chain
                    self._memory.set(self._memory_key(fingerprint, urn), chain)
            except Exception as exc:
                self._note_redis_fail(exc, op="hmget")
                missing_after_redis = list(urns)
        else:
            missing_after_redis = list(urns)
        # Memory fallback for whatever Redis didn't have / failed on.
        for urn in missing_after_redis:
            chain = self._memory.get(self._memory_key(fingerprint, urn))
            if chain is not None:
                out[urn] = chain
        return out

    # ----- Write API -------------------------------------------------------

    async def set(self, urn: str, chain: List[str], *, fingerprint: str) -> None:
        encoded = self._encode(chain)
        # Memory always written so reads have a fallback even if Redis fails.
        self._memory.set(self._memory_key(fingerprint, urn), chain)
        if self._redis_alive():
            try:
                hash_key = self._hash_key(fingerprint)
                await self._redis.hset(hash_key, urn, encoded)
                # Touch TTL on the hash. Spanner's recommendation is to
                # set on first write rather than per-field; we do the
                # cheap call every set and let Redis dedupe.
                await self._redis.expire(hash_key, self._ttl_s)
                self._note_redis_ok()
            except Exception as exc:
                self._note_redis_fail(exc, op="hset")

    async def mset(
        self,
        mapping: Dict[str, List[str]],
        *,
        fingerprint: str,
    ) -> None:
        if not mapping:
            return
        for urn, chain in mapping.items():
            self._memory.set(self._memory_key(fingerprint, urn), chain)
        if self._redis_alive():
            try:
                encoded = {urn: self._encode(chain) for urn, chain in mapping.items()}
                hash_key = self._hash_key(fingerprint)
                await self._redis.hmset(hash_key, encoded)
                await self._redis.expire(hash_key, self._ttl_s)
                self._note_redis_ok()
            except Exception as exc:
                self._note_redis_fail(exc, op="hmset")

    async def invalidate(self, urn: str, *, fingerprint: str) -> None:
        """Drop one URN from both layers. Called from on_containment_changed."""
        self._memory.delete(self._memory_key(fingerprint, urn))
        if self._redis_alive():
            try:
                await self._redis.hdel(self._hash_key(fingerprint), urn)
                self._note_redis_ok()
            except Exception as exc:
                self._note_redis_fail(exc, op="hdel")

    async def invalidate_fingerprint(self, *, fingerprint: str) -> None:
        """Drop the entire namespace+fingerprint hash.

        Used when a provider explicitly resets (e.g. a purge job) rather
        than relying on natural cache namespacing via a fingerprint
        change.
        """
        self._memory.delete_prefix(self._memory_prefix(fingerprint))
        if self._redis_alive():
            try:
                await self._redis.delete(self._hash_key(fingerprint))
                self._note_redis_ok()
            except Exception as exc:
                self._note_redis_fail(exc, op="delete")

    # ----- Health bookkeeping ----------------------------------------------

    def _redis_alive(self) -> bool:
        return self._redis is not None and self._redis_failures < self._redis_failure_threshold

    def _note_redis_ok(self) -> None:
        if self._redis_failures:
            logger.info("ancestor cache: redis recovered after %d failures", self._redis_failures)
            self._redis_failures = 0

    def _note_redis_fail(self, exc: BaseException, *, op: str) -> None:
        self._redis_failures += 1
        if self._redis_failures == self._redis_failure_threshold:
            logger.warning(
                "ancestor cache: redis disabled after %d consecutive failures (last op=%s err=%s)",
                self._redis_failures, op, exc,
            )
        else:
            logger.debug("ancestor cache: redis op=%s failed: %s", op, exc)

    # ----- Encoding -------------------------------------------------------

    @staticmethod
    def _encode(chain: List[str]) -> str:
        return json.dumps(chain, separators=(",", ":"))

    @staticmethod
    def _decode(raw: Any) -> List[str]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return data if isinstance(data, list) else []
