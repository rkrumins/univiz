"""In-flight request deduplication ("single-flight") for FastAPI handlers.

Solves the classic thundering-herd problem: when N concurrent requests
arrive for the same expensive read with a cold cache, naive code fires
N identical downstream calls. This module makes the first caller run
the work and the other N-1 await the same result.

Use case in this codebase: hot read endpoints like
``GET /cached-stats``, ``GET /views/popular``, ``GET /views/facets``
where the same key is read by many users at the same moment.

API::

    sf = SingleFlight()

    async def handler():
        return await sf.run(("cached-stats", ds_id), expensive_fetch)

The caller passes a coroutine factory (``Callable[[], Awaitable[T]]``)
rather than an already-awaited coroutine, so followers don't end up
re-invoking the work themselves.

Design notes:

* **Per-key TTL** stamped on every entry. If a leader coroutine is
  somehow lost before reaching ``finally`` (cancellation racing with
  task GC), the next caller after ``ttl_seconds`` re-enters as a
  fresh leader instead of waiting on a dead future. This is
  defence-in-depth — the ``try/finally pop`` in ``run`` already
  cleans up the happy path, exception path, and cancellation path.
* **No background sweep.** Eviction is lazy, checked on the same key
  the caller is interested in. O(1) per request; no scheduled tasks
  hidden in the module.
* **No new dependencies.** Pure stdlib: ``asyncio`` + ``time``.
* **GET-only is enforced at the call site**, not here — this module
  is method-agnostic. Callers building keys from request bodies must
  guarantee bounded cardinality themselves (otherwise an attacker
  could exhaust memory by crafting unique POST bodies).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional, Tuple, TypeVar

T = TypeVar("T")


def _swallow_unretrieved_exception(fut: "asyncio.Future[Any]") -> None:
    """Mark an exception-bearing future as "retrieved" so the asyncio
    GC doesn't log ``Future exception was never retrieved`` when no
    follower happens to be awaiting at the moment the leader raises.

    Followers that DO await still receive the same exception through
    the normal ``await fut`` → ``fut.result()`` path; calling
    ``fut.exception()`` here doesn't consume the stored exception, it
    only updates asyncio's internal tracking bit.
    """
    if fut.cancelled():
        return
    # ``exception()`` raises only on cancelled futures (handled above)
    # or futures that aren't done — neither can happen in a done
    # callback. Safe to invoke unconditionally.
    fut.exception()


class SingleFlight:
    """Coalesce concurrent calls with the same key into one execution.

    Thread-unsafe by design — instances are intended to be process-
    wide singletons used from a single asyncio event loop. With
    multiple gunicorn workers each gets its own SingleFlight, so
    dedup is per-worker (the layered cache in WS-2's ETag plumbing
    handles cross-worker dedup at a coarser grain).

    ``ttl_seconds`` is the upper bound on how long a leader can hold
    a key before a fresh caller pre-empts. Tune up if your longest
    expected operation is longer; default 30s covers anything that
    isn't a runaway query (and runaway queries should fail their
    own timeouts before they hit this).
    """

    __slots__ = ("_in_flight", "_ttl")

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        # value = (future, deadline_monotonic). Deadline lets us evict
        # stale entries lazily without a background sweeper.
        self._in_flight: Dict[Hashable, Tuple[asyncio.Future[Any], float]] = {}
        self._ttl = float(ttl_seconds)

    def in_flight_count(self) -> int:
        """How many keys are currently in flight. Exposed for tests
        and the eventual ``/internal/metrics/single-flight`` endpoint
        (not built here — kept minimal until there's a need)."""
        return len(self._in_flight)

    async def run(
        self,
        key: Hashable,
        fn: Callable[[], Awaitable[T]],
    ) -> T:
        """Run ``fn()`` once for ``key`` regardless of concurrency.

        First caller for a key runs ``fn``; concurrent callers
        for the same key await the leader's result. Returns whatever
        the leader returned, or raises whatever the leader raised
        (the same exception instance is shared across all waiters).

        ``key`` must be hashable. Conventional shape:
        ``("endpoint", id, ...filters)`` — tuples are hashable when
        their elements are. Don't include unbounded inputs (full
        query strings, request bodies) without a normalisation step.
        """
        now = time.monotonic()
        existing = self._in_flight.get(key)
        # Lazy TTL eviction: if the prior entry is past its deadline,
        # treat it as gone and re-enter as a fresh leader. Cheap
        # because we check only the key the caller is interested in.
        if existing is not None and existing[1] < now:
            self._in_flight.pop(key, None)
            existing = None

        if existing is not None:
            return await existing[0]

        # Leader path. Hold a ref to the future before doing any await
        # so a concurrent caller arriving between this line and the
        # next ``await`` sees us and joins. ``setdefault`` would also
        # close the race, but we already proved the key is absent
        # under the same call (no yield point between the lookup and
        # the assignment below).
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        # Mark any exception we eventually set on the future as
        # "retrieved" so asyncio's GC doesn't log a spurious warning
        # when there are zero followers. The exception still surfaces
        # to any follower that joins and awaits the future — this
        # callback just silences the no-one-awaited noise.
        future.add_done_callback(_swallow_unretrieved_exception)
        self._in_flight[key] = (future, now + self._ttl)

        try:
            result = await fn()
        except asyncio.CancelledError:
            # Leader cancelled mid-flight (e.g. client disconnected
            # or an asyncio.timeout fired). Propagate to followers
            # so they don't hang on a never-resolving future, then
            # let the cancellation continue up our own stack.
            if not future.done():
                future.cancel()
            raise
        except BaseException as exc:
            # Any other exception: share it with followers. Note we
            # catch ``BaseException`` (not just ``Exception``) so that
            # ``SystemExit`` / ``KeyboardInterrupt`` also wake followers
            # cleanly before re-raising.
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            # Clear the key under every exit path. Use ``pop`` with a
            # default so a defensive concurrent eviction (TTL) can't
            # raise a KeyError here.
            self._in_flight.pop(key, None)


# Process-wide singletons for the three endpoints that opt in. Keep
# instances small and scoped to a use case rather than one giant
# SingleFlight — collisions across unrelated endpoints would obscure
# the in_flight_count metric and complicate TTL tuning.
read_stats_sf = SingleFlight(ttl_seconds=30.0)
read_views_sf = SingleFlight(ttl_seconds=15.0)


def normalised_principal(user_id: Optional[str]) -> str:
    """Stable key fragment for the authenticated principal.

    Per-user keys are required when the underlying response varies by
    user (e.g. ``is_favourited`` per-user flags in popular views).
    Anonymous callers share a single key, which is the correct
    behaviour: they see the same data and should be deduped together.
    """
    return user_id or "anon"


__all__ = [
    "SingleFlight",
    "read_stats_sf",
    "read_views_sf",
    "normalised_principal",
]
