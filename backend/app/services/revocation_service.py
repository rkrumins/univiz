"""RevocationService — Redis-backed session revocation.

When permission claims are embedded in the JWT they are valid for the
duration of the access token (5 min by default). To make forced
revocation (suspend user, remove from group, drop binding) take effect
sooner than that, every session is tagged with a random ``sid`` claim
and we maintain a Redis SET of revoked sids. The ``requires(...)``
dependency checks the set on every request and force-logs-out any
session whose sid is present.

Keys live under ``rbac:revoked:<sid>`` and self-expire via Redis TTL,
so no cron is required.

This module hides the Redis client behind a class so tests can swap in
an in-memory fake. Production code goes through the singleton
constructed by ``get_revocation_service()``.

Phase 1 ships this service alongside the migration but does NOT wire
it into endpoints — Phase 2 turns it on per area.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Protocol

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Access-token TTL drives how long a revocation entry needs to live —
# we keep it for TTL + a buffer so a request that arrives at the very
# end of the token's life still finds the entry. The default mirrors
# the design plan (5 min access TTL → 6 min revocation TTL).
_DEFAULT_REVOCATION_TTL_SECONDS = 360
REVOCATION_TTL_SECONDS: int = int(
    os.getenv("RBAC_REVOCATION_TTL_SECONDS", str(_DEFAULT_REVOCATION_TTL_SECONDS))
)

_KEY_PREFIX = "rbac:revoked:"
_USER_SIDS_PREFIX = "rbac:user_sids:"


def _key(sid: str) -> str:
    return f"{_KEY_PREFIX}{sid}"


def _user_sids_key(user_id: str) -> str:
    return f"{_USER_SIDS_PREFIX}{user_id}"


# ── Backend protocol (so tests can swap in a fake) ───────────────────

class RevocationBackend(Protocol):
    async def exists(self, key: str) -> bool: ...
    async def set_with_ttl(self, key: str, ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    # user → sids reverse index (a set keyed by user, whole-key TTL).
    async def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None: ...
    async def set_members(self, key: str) -> set[str]: ...
    async def health(self) -> bool: ...


# ── Real Redis backend ────────────────────────────────────────────────

class RedisBackend:
    """Thin wrapper around redis.asyncio so the surface stays small.

    We hold one client per process. Connection errors are caught and
    re-raised as ``RevocationBackendError`` so the caller can decide on
    the fail-open / fail-closed policy.
    """
    def __init__(self, url: str):
        # Lazy import: redis is in requirements.txt but we do not want
        # an import-time failure to break unrelated parts of the app
        # if the dependency is missing in some environments (e.g.
        # static analysis pre-install).
        import redis.asyncio as redis_async  # noqa: WPS433
        self._client = redis_async.from_url(url, decode_responses=True)

    async def exists(self, key: str) -> bool:
        try:
            return bool(await self._client.exists(key))
        except Exception as exc:  # broad on purpose — Redis errors → backend error
            raise RevocationBackendError(str(exc)) from exc

    async def set_with_ttl(self, key: str, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, "1", ex=ttl_seconds)
        except Exception as exc:
            raise RevocationBackendError(str(exc)) from exc

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception as exc:
            raise RevocationBackendError(str(exc)) from exc

    async def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None:
        try:
            await self._client.sadd(key, member)
            # Refresh the whole-key TTL on every add so an active user's
            # index outlives their most recent session by the buffer.
            await self._client.expire(key, ttl_seconds)
        except Exception as exc:
            raise RevocationBackendError(str(exc)) from exc

    async def set_members(self, key: str) -> set[str]:
        try:
            return set(await self._client.smembers(key))
        except Exception as exc:
            raise RevocationBackendError(str(exc)) from exc

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False


class InMemoryBackend:
    """Fallback used in tests and local dev when Redis is not reachable.

    Not safe across processes; do not use in production. The service
    initialiser logs a loud warning when this backend is selected.
    """
    def __init__(self) -> None:
        self._set: set[str] = set()
        self._sets: dict[str, set[str]] = {}

    async def exists(self, key: str) -> bool:
        return key in self._set

    async def set_with_ttl(self, key: str, ttl_seconds: int) -> None:
        # TTL is ignored in the fake; tests that need expiry should
        # call ``delete`` explicitly.
        self._set.add(key)

    async def delete(self, key: str) -> None:
        self._set.discard(key)
        self._sets.pop(key, None)

    async def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None:
        # TTL ignored in the fake (see set_with_ttl).
        self._sets.setdefault(key, set()).add(member)

    async def set_members(self, key: str) -> set[str]:
        return set(self._sets.get(key, ()))

    async def health(self) -> bool:
        return True


class RevocationBackendError(Exception):
    """Raised when the Redis backend rejects an operation. Callers
    decide fail-open vs fail-closed based on the operation context."""


# ── Service ──────────────────────────────────────────────────────────

class RevocationService:
    """Owns the Redis client and exposes the per-event helpers used by
    higher-level code (``users.suspend``, ``binding.create``, etc.).

    Phase 1 includes the helpers but their callers (the user / group /
    binding endpoints) start invoking them in Phase 2. Shipping the
    helpers now means the backend exists and is unit-tested by the time
    Phase 2 wires them.
    """
    def __init__(
        self,
        backend: RevocationBackend,
        *,
        ttl_seconds: int = REVOCATION_TTL_SECONDS,
    ):
        self._backend = backend
        self._ttl = ttl_seconds

    # Used by ``requires()`` per request.
    async def is_revoked(self, sid: str) -> bool:
        if not sid:
            return False
        return await self._backend.exists(_key(sid))

    # Granular revocation: caller knows the exact sid.
    async def revoke_session(self, sid: str) -> None:
        if not sid:
            return
        await self._backend.set_with_ttl(_key(sid), self._ttl)

    async def revoke_sessions(self, sids: Iterable[str]) -> None:
        for sid in sids:
            await self.revoke_session(sid)

    # Session tracking: every login/refresh mints a fresh sid; record
    # it under the user's reverse index so a later coarse revocation
    # can find every live session for that user. The set carries a
    # whole-key TTL (refreshed on each add) equal to the revocation
    # window, so stale sids self-expire — a revoked sid whose access
    # token has already lapsed is a harmless no-op anyway.
    async def record_session(self, user_id: str, sid: str) -> None:
        if not user_id or not sid:
            return
        await self._backend.add_to_set(
            _user_sids_key(user_id), sid, self._ttl
        )

    # Coarse revocation: caller knows the user but not their sids.
    # Reads the reverse index, revokes every sid in it, then drops the
    # index so a re-login starts a clean set.
    async def revoke_all_user_sessions(self, user_id: str) -> None:
        if not user_id:
            return
        key = _user_sids_key(user_id)
        sids = await self._backend.set_members(key)
        for sid in sids:
            await self.revoke_session(sid)
        await self._backend.delete(key)
        logger.info(
            "revoke_all_user_sessions: user=%s revoked %d session(s)",
            user_id, len(sids),
        )

    async def health(self) -> bool:
        return await self._backend.health()


# ── Singleton wiring ──────────────────────────────────────────────────

_INSTANCE: Optional[RevocationService] = None


def get_revocation_service() -> RevocationService:
    """Return the process-singleton service.

    Falls back to ``InMemoryBackend`` (with a warning) if Redis cannot
    be constructed at import time — this keeps unit tests and local
    dev usable without a running Redis.
    """
    global _INSTANCE
    if _INSTANCE is None:
        try:
            backend: RevocationBackend = RedisBackend(REDIS_URL)
        except ImportError:
            logger.warning(
                "redis library not available — using InMemoryBackend. "
                "RBAC revocation will not survive process restarts."
            )
            backend = InMemoryBackend()
        _INSTANCE = RevocationService(backend)
    return _INSTANCE


def configure_revocation_service(service: RevocationService) -> None:
    """Test-only: install a custom service instance.

    Production code must not call this — it bypasses the URL and TTL
    config. Used by the test suite to install a fake-backed service.
    """
    global _INSTANCE
    _INSTANCE = service


__all__ = [
    "RevocationService",
    "RevocationBackend",
    "RedisBackend",
    "InMemoryBackend",
    "RevocationBackendError",
    "get_revocation_service",
    "configure_revocation_service",
    "REDIS_URL",
    "REVOCATION_TTL_SECONDS",
]
