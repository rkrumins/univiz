"""
GraphCache — Redis-backed read-side cache for hot graph endpoints.

The two endpoints `/children-with-edges` and `/edges/aggregated` carry
the bulk of the read load. Each invocation issues a Cypher / GQL query
against FalkorDB or Spanner. Under 100 concurrent users opening the
same view, those endpoints fire identical queries against a graph
provider whose Cypher thread is single-threaded — the documented cause
of 300-400% CPU spikes that lock up the app for everyone.

GraphCache wraps the provider call with two layers of protection:

1. **Redis response cache** keyed by (workspace, data_source, gen,
   endpoint, params_hash). First request computes; the next N within
   the TTL window read from Redis. `gen` is a per-(workspace, ds)
   counter bumped on every write — old cache entries become unreachable
   on the next read and TTL-expire on their own. This sidesteps the
   "two hard problems" of surgical invalidation.

2. **In-process singleflight** keyed by the same cache key. When 50
   concurrent requests inside the same pod ask for the same children,
   only one calls the provider; the rest await the shared Future. This
   protects the provider during the cold-cache window — the moment
   right after a gen bump, after pod start, or after key expiry.

Cross-process singleflight via Redis lease is a Phase 1 spike and
NOT included here. The in-process variant covers same-pod fan-out;
cross-pod fan-out is bounded by the per-(provider, graph) semaphore
in the ProviderManager (default 8). The combination is sufficient for
the multi-tenant 100-user target without paying the 1-2 RTT cost of a
distributed lock on every cache miss.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

from pydantic import BaseModel
from redis import asyncio as aioredis
from redis.exceptions import RedisError

from backend.app.services.aggregation.redis_client import get_redis

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_KEY_PREFIX = "graphcache:v1"
_GEN_PREFIX = "graphcache:gen"

# Per-endpoint TTLs (seconds). The plan calls for 30s on children and 60s
# on aggregated — tunable via env so we can dial up under verified low
# write rates or dial down if we see staleness complaints.
_DEFAULT_CHILDREN_TTL = int(os.getenv("GRAPH_CACHE_CHILDREN_TTL_S", "30"))
_DEFAULT_AGGREGATED_TTL = int(os.getenv("GRAPH_CACHE_AGGREGATED_TTL_S", "60"))
# Short TTL for empty/404 results — absorbs herds asking for the same
# missing URN without committing to caching nonsense for long.
_NEGATIVE_TTL = int(os.getenv("GRAPH_CACHE_NEGATIVE_TTL_S", "5"))

# Per-endpoint kill switches. Default OFF so the cache rolls out behind a
# feature flag. Operations turns them on per-endpoint after instrumenting.
def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")

ENDPOINT_CHILDREN = "children-with-edges"
ENDPOINT_AGGREGATED = "aggregated"

_ENABLED_ENDPOINTS = {
    ENDPOINT_CHILDREN: _flag("GRAPH_CACHE_ENABLED_CHILDREN", default=False),
    ENDPOINT_AGGREGATED: _flag("GRAPH_CACHE_ENABLED_AGGREGATED", default=False),
}


@dataclass(frozen=True)
class CacheScope:
    """Identifies the (workspace, data_source) the cache entry belongs to.

    workspace_id is required — multi-tenant correctness depends on it.
    data_source_id is optional because some workspaces have a default
    data source resolved server-side; we coerce missing to the literal
    empty string so the key is stable across requests that omit it.
    """
    workspace_id: str
    data_source_id: str = ""


class GraphCache:
    """Singleton cache wrapper. Get the instance via `get_graph_cache()`."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        # In-process singleflight: key → Future holding the computed result.
        # Concurrent callers awaiting an in-flight future read the same
        # answer with no extra provider work.
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    # ─── Public surface ───────────────────────────────────────────────

    def is_enabled(self, endpoint: str) -> bool:
        """Per-endpoint feature flag check. Cheap; no Redis I/O."""
        return _ENABLED_ENDPOINTS.get(endpoint, False)

    async def get_or_compute(
        self,
        scope: CacheScope,
        endpoint: str,
        params: dict[str, Any],
        compute: Callable[[], Awaitable[T]],
        model_cls: type[T],
        ttl_seconds: Optional[int] = None,
    ) -> T:
        """Fetch from cache, falling back to `compute()` on miss.

        The result of `compute()` MUST be a Pydantic v2 model instance
        (we serialize via `model_dump_json` for stable, schema-aware
        round-tripping). On any Redis error we fall through to direct
        provider compute — the cache must never become a hard dependency.
        """
        if not self.is_enabled(endpoint):
            return await compute()

        try:
            gen = await self._get_generation(scope)
            cache_key = _build_key(scope, gen, endpoint, params)
        except RedisError as exc:
            logger.warning("graph_cache: gen read failed (%s); bypassing cache", exc)
            return await compute()

        # ── 1. Redis cache lookup ─────────────────────────────────────
        try:
            cached = await self._redis.get(cache_key)
        except RedisError as exc:
            logger.warning("graph_cache: GET failed (%s); bypassing cache", exc)
            return await compute()

        if cached is not None:
            try:
                return model_cls.model_validate_json(cached)
            except Exception as exc:
                # Bad payload (schema drift?) — log and treat as miss. The
                # offending key will be overwritten by the compute below.
                logger.warning(
                    "graph_cache: deserialize failed for %s (%s); recomputing",
                    cache_key, exc,
                )

        # ── 2. In-process singleflight ────────────────────────────────
        # Coalesce concurrent callers in this pod. Outside-pod fan-out is
        # bounded by the provider semaphore, so this is sufficient at our
        # current scale.
        existing = self._inflight.get(cache_key)
        if existing is not None:
            try:
                return await asyncio.shield(existing)
            except Exception:
                # Leader failed — fall through to recompute below.
                pass

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._inflight[cache_key] = fut
        try:
            result = await compute()
            await self._set(cache_key, result, ttl_seconds, endpoint)
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(cache_key, None)

    async def bump_generation(self, scope: CacheScope) -> None:
        """Invalidate every cached entry under `scope` by bumping the
        per-scope generation counter. Old keys become unreachable on the
        next read and TTL-expire on their own — no SCAN/DEL needed.

        Safe to call from a write path even with the cache feature flag
        off; INCR on a non-existent key just starts it at 1.
        """
        try:
            await self._redis.incr(_gen_key(scope))
        except RedisError as exc:
            logger.warning(
                "graph_cache: generation bump failed for %s (%s); "
                "stale entries may persist until TTL expiry",
                scope, exc,
            )

    # ─── Internals ────────────────────────────────────────────────────

    async def _get_generation(self, scope: CacheScope) -> int:
        """Read the current generation counter for `scope`. Returns 0
        when never set (which yields a stable initial key)."""
        raw = await self._redis.get(_gen_key(scope))
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            # Garbage in the counter slot — treat as fresh epoch. Don't
            # try to repair: write paths will overwrite via INCR.
            return 0

    async def _set(
        self,
        cache_key: str,
        result: BaseModel,
        ttl_seconds: Optional[int],
        endpoint: str,
    ) -> None:
        """Serialize and persist `result`. Failures are swallowed — the
        compute already succeeded, so failing the response on a write
        error would be a self-inflicted regression."""
        ttl = _resolve_ttl(ttl_seconds, endpoint)
        if _is_empty_result(result):
            ttl = _NEGATIVE_TTL
        try:
            payload = result.model_dump_json(by_alias=True)
            await self._redis.set(cache_key, payload, ex=ttl)
        except (RedisError, Exception) as exc:
            logger.warning("graph_cache: SET failed (%s)", exc)


# ─── Module-level helpers ──────────────────────────────────────────────

def _gen_key(scope: CacheScope) -> str:
    return f"{_GEN_PREFIX}:{scope.workspace_id}:{scope.data_source_id}"


def _build_key(scope: CacheScope, gen: int, endpoint: str, params: dict[str, Any]) -> str:
    """Build a cache key. We hash params (not raw-include them) so the
    key length is bounded — `params` for /edges/aggregated can carry
    thousands of source URNs."""
    digest = hashlib.sha1(
        json.dumps(params, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()
    return f"{_KEY_PREFIX}:{scope.workspace_id}:{scope.data_source_id}:{gen}:{endpoint}:{digest}"


def _resolve_ttl(explicit: Optional[int], endpoint: str) -> int:
    if explicit is not None:
        return explicit
    if endpoint == ENDPOINT_CHILDREN:
        return _DEFAULT_CHILDREN_TTL
    if endpoint == ENDPOINT_AGGREGATED:
        return _DEFAULT_AGGREGATED_TTL
    return _DEFAULT_CHILDREN_TTL


def _is_empty_result(result: BaseModel) -> bool:
    """Detect "empty" responses worth caching only briefly. Currently:
    a ChildrenWithEdgesResult with no children, or an AggregatedEdgeResult
    with no aggregated edges. Returning True shortens the TTL to the
    negative-cache window so a transient miss doesn't pin the empty
    answer for 30-60s."""
    children = getattr(result, "children", None)
    if isinstance(children, list) and len(children) == 0:
        return True
    aggregated = getattr(result, "aggregated_edges", None)
    if isinstance(aggregated, list) and len(aggregated) == 0:
        return True
    return False


# ─── Singleton accessor ────────────────────────────────────────────────

_cache: Optional[GraphCache] = None


def get_graph_cache() -> GraphCache:
    """Return the process-wide GraphCache. Lazy-initialised on first use
    so test code can patch `get_redis()` before this fires."""
    global _cache
    if _cache is None:
        _cache = GraphCache(get_redis())
    return _cache


def reset_graph_cache_for_tests() -> None:
    """Drop the singleton so a fresh fixture can install its own."""
    global _cache
    _cache = None
