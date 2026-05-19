"""
Workspace-keyed token-bucket rate limiter for the hot graph endpoints.

The per-(provider, graph) semaphore in ``ProviderManager`` caps fan-out
at the resource layer, but it treats every request equally — one
workspace running "expand all" on a 500-node hierarchy can burn the
global slot budget and starve every other tenant on the same data
source. At the 100+ concurrent multi-tenant target we cannot rely on
goodwill: each workspace gets its own bucket, and a noisy tenant only
slows itself.

The bucket is a classic token bucket evaluated atomically inside Redis
via a Lua script: ``rate`` tokens/second refill, ``capacity`` burst,
``cost`` charged per request (always 1 today; reserved for endpoint
weighting). On deny the caller receives a wait hint we forward into
the ``Retry-After`` header — the frontend's ``fetchWithTimeout``
honors it transparently, so 429s degrade to a brief pause rather than
an error toast.

Defaults are intentionally generous: 30 r/s sustained on the read-heavy
``/children-with-edges`` with 60-burst, 5 r/s on the expensive
``/edges/aggregated`` with 10-burst. They are tunable per-endpoint via
env vars so we can dial down under load without a redeploy. The whole
limiter is gated behind ``FAIR_SHARE_ENABLED`` (default off) for a
phased rollout — Phase 0 already covers most fan-out via frontend caps;
this is the multi-tenant insurance policy.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

from redis import asyncio as aioredis
from redis.exceptions import RedisError

from backend.app.services.aggregation.redis_client import get_redis
from backend.common.adapters import ProviderBusy

logger = logging.getLogger(__name__)

# Endpoint identifiers. Match `graph_cache.py` so observability lines up
# across the two modules.
ENDPOINT_CHILDREN = "children-with-edges"
ENDPOINT_AGGREGATED = "aggregated"


@dataclass(frozen=True)
class BucketConfig:
    """Static per-endpoint token-bucket parameters."""
    rate_per_sec: float
    burst: int


def _load_config(prefix: str, default_rate: float, default_burst: int) -> BucketConfig:
    try:
        rate = float(os.getenv(f"FAIR_SHARE_{prefix}_RATE", str(default_rate)))
    except ValueError:
        rate = default_rate
    try:
        burst = int(os.getenv(f"FAIR_SHARE_{prefix}_BURST", str(default_burst)))
    except ValueError:
        burst = default_burst
    return BucketConfig(rate_per_sec=max(0.1, rate), burst=max(1, burst))


_CONFIGS: dict[str, BucketConfig] = {
    ENDPOINT_CHILDREN: _load_config("CHILDREN", 30.0, 60),
    ENDPOINT_AGGREGATED: _load_config("AGGREGATED", 5.0, 10),
}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


_ENABLED = _flag("FAIR_SHARE_ENABLED", default=False)


# Atomic refill-and-take. KEYS[1] = bucket hash. ARGV are rate, capacity,
# cost, now_ms. Returns {allowed_int, retry_after_ms}. Keeping it short
# matters — every read of a guarded endpoint pays one EVALSHA round
# trip; we want it to land in <0.5ms on a co-located Redis.
_LUA_TAKE = """
local tokens = tonumber(redis.call("HGET", KEYS[1], "tokens"))
local ts = tonumber(redis.call("HGET", KEYS[1], "ts"))
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now_ms = tonumber(ARGV[4])

if tokens == nil or ts == nil then
  tokens = capacity
  ts = now_ms
end

local elapsed_s = (now_ms - ts) / 1000.0
if elapsed_s > 0 then
  tokens = math.min(capacity, tokens + elapsed_s * rate)
end

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local deficit = cost - tokens
  retry_after_ms = math.ceil(deficit / rate * 1000)
end

redis.call("HSET", KEYS[1], "tokens", tokens, "ts", now_ms)
local refill_time_s = math.ceil(capacity / rate)
local ttl = refill_time_s + 10
if ttl < 60 then ttl = 60 end
redis.call("EXPIRE", KEYS[1], ttl)

return {allowed, retry_after_ms}
"""


@dataclass(frozen=True)
class TakeResult:
    """Outcome of a single ``take()`` against a bucket."""
    allowed: bool
    retry_after_seconds: int


class WorkspaceTokenBucket:
    """Process-wide singleton wrapping the Redis-backed bucket. Use via
    :func:`get_fair_share`."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        # `register_script` returns an AsyncScript whose `__call__` issues
        # EVALSHA + EVAL fallback on first use — no manual SHA caching.
        self._take_script = redis.register_script(_LUA_TAKE)

    @staticmethod
    def is_enabled() -> bool:
        return _ENABLED

    async def take(
        self,
        endpoint: str,
        workspace_id: str,
        cost: int = 1,
    ) -> TakeResult:
        """Attempt to charge ``cost`` tokens to (endpoint, workspace_id).

        Returns ``allowed=True`` when there were enough tokens — caller
        proceeds. ``allowed=False`` carries ``retry_after_seconds`` ≥ 1
        rounded up from the time-to-refill so the client's exponential
        backoff doesn't churn.

        On any Redis error we **fail open** — degrading to no rate
        limiting is preferable to refusing every request when Redis
        flaps. The provider-level semaphore is still in place as a hard
        backstop.
        """
        cfg = _CONFIGS.get(endpoint)
        if cfg is None:
            return TakeResult(True, 0)
        if not workspace_id:
            # Unscoped requests bypass — `/api/v1` legacy routes that
            # carry no workspace context. The fair-share guard only
            # applies to multi-tenant traffic on the `/v1/{ws_id}/...`
            # paths.
            return TakeResult(True, 0)

        key = f"fairshare:{endpoint}:{workspace_id}"
        now_ms = int(time.time() * 1000)
        try:
            result = await self._take_script(
                keys=[key],
                args=[cfg.rate_per_sec, cfg.burst, cost, now_ms],
            )
        except RedisError as exc:
            logger.warning(
                "fair_share: take failed for %s/%s (%s); failing open",
                endpoint, workspace_id, exc,
            )
            return TakeResult(True, 0)

        allowed_int, retry_ms = int(result[0]), int(result[1])
        # Always at least 1s so clients don't busy-loop on retry, even
        # if the math says "60ms" — the rate-limit signal exists to
        # give the underlying provider room to drain.
        retry_s = max(1, math.ceil(retry_ms / 1000)) if not allowed_int else 0
        return TakeResult(allowed=bool(allowed_int), retry_after_seconds=retry_s)

    async def enforce(self, endpoint: str, workspace_id: str) -> None:
        """Take a token; raise :class:`ProviderBusy` on deny so the
        existing 429+Retry-After handler in ``main.py`` returns a
        properly-formed response."""
        outcome = await self.take(endpoint, workspace_id)
        if outcome.allowed:
            return
        raise ProviderBusy(
            provider_name=f"workspace:{workspace_id}",
            reason=f"per-workspace rate limit exceeded for {endpoint}",
            retry_after_seconds=outcome.retry_after_seconds,
        )


# ─── Singleton ────────────────────────────────────────────────────────

_bucket: Optional[WorkspaceTokenBucket] = None


def get_fair_share() -> WorkspaceTokenBucket:
    """Return the process-wide fair-share bucket. Lazy so test code can
    patch ``get_redis`` before initialization."""
    global _bucket
    if _bucket is None:
        _bucket = WorkspaceTokenBucket(get_redis())
    return _bucket


def reset_fair_share_for_tests() -> None:
    global _bucket
    _bucket = None
