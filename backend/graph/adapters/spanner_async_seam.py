"""Async/sync seam for the Spanner client.

The official ``google-cloud-spanner`` high-level API
(``Client``/``Database``/``Snapshot``/``Batch``) is synchronous. The
async low-level ``SpannerAsyncClient`` exists at the gRPC layer but
lacks session-pooling, transparent streaming-result resumption, and
``Aborted``-retry ergonomics. This module is the single place that
bridges the two worlds.

When Google ships a high-level async API, this is the only file that
needs to change.

Cancellation contract
---------------------
``anyio.to_thread.run_sync`` defaults to ``abandon_on_cancel=False`` —
when the calling task is cancelled (e.g. ``asyncio.wait_for`` fires)
the worker thread continues executing the sync call to completion. For
this provider that creates a thread leak and a misleading "deadline"
guarantee on every Spanner brownout: the awaiter unblocks but the gRPC
call lives on, holding a session and a slot in the threadpool limiter.

The seam exposes the cancellation knob explicitly via ``read_only``:

* ``read_only=True``  — pass ``abandon_on_cancel=True`` to the worker.
  The cancelled task returns immediately; the gRPC call keeps running
  in the background but is bounded by the *server-side* timeout the
  caller passed via ``snapshot.execute_sql(..., timeout=budget)`` (in
  ``_execute_query``). The leaked thread exits cleanly within
  ``budget`` seconds and the limiter slot is then released.

* ``read_only=False`` — keep the default ``abandon_on_cancel=False``.
  Read-write transactions are NOT safe to abandon: ``run_in_transaction``
  may be inside a commit RPC the moment we cancel; abandoning could
  produce a "phantom commit" the application has stopped expecting.
  Writes rely on ``run_in_transaction(..., timeout_secs=budget)`` to
  bound the inner Aborted-retry loop and on ``DeadlineGuard`` to bound
  the asyncio side. Net effect: at most ``budget`` seconds of
  threadpool occupancy per cancelled write.

Concurrency limit
-----------------
A dedicated :class:`anyio.CapacityLimiter` bounds Spanner concurrency
in this process so a Spanner brownout cannot exhaust the global AnyIO
threadpool (default 40, shared with every other ``to_thread`` consumer
including Alembic). The limiter is sized from the
``SPANNER_THREADPOOL_LIMIT`` env var; the default is 100, matching the
Spanner client's default session pool. Override per environment if you
also tune ``Database._pool``.

Observability
-------------
Calls flow through ``to_thread`` which is the canonical chokepoint. We
emit one OpenTelemetry span per call covering both the threadpool wait
and the underlying gRPC round-trip; this gives traces a single
attributable span per Spanner operation rather than two opaque awaits.

The OpenTelemetry import is optional: when the package isn't installed,
``to_thread`` falls back to a plain ``anyio.to_thread.run_sync`` call.
This keeps the seam zero-cost in environments that don't ship traces
and avoids forcing a hard dependency on the otel SDK in production.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional, TypeVar

import anyio

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Optional OpenTelemetry tracer — resolved once at import time.
# ---------------------------------------------------------------------------

try:  # pragma: no cover  -- exercised only when otel is installed.
    from opentelemetry import trace as _otel_trace  # type: ignore
    _tracer = _otel_trace.get_tracer("backend.graph.adapters.spanner")
except ImportError:  # pragma: no cover  -- the common case in dev/test.
    _tracer = None


# ---------------------------------------------------------------------------
# Dedicated threadpool limiter.
# ---------------------------------------------------------------------------
# Rationale: AnyIO's default `current_default_thread_limiter()` is shared
# across the whole process (default 40). At enterprise scale a Spanner
# brownout would saturate it and starve every other to_thread consumer
# (Alembic, FalkorDB, etc.). The dedicated limiter isolates the blast
# radius and makes the cap tunable per-deployment.

def _read_limit() -> int:
    raw = os.getenv("SPANNER_THREADPOOL_LIMIT")
    if not raw:
        return 100
    try:
        n = int(raw)
        return n if n > 0 else 100
    except ValueError:
        logger.warning(
            "spanner: SPANNER_THREADPOOL_LIMIT=%r is not an int; using 100",
            raw,
        )
        return 100


_SPANNER_LIMITER: Optional[anyio.CapacityLimiter] = None


def _get_limiter() -> anyio.CapacityLimiter:
    """Return (and lazily build) the process-wide Spanner threadpool limiter.

    Lazy because :class:`anyio.CapacityLimiter` requires a running event
    loop to construct; importing this module from a sync entry point
    (alembic, scripts) would otherwise crash.
    """
    global _SPANNER_LIMITER
    if _SPANNER_LIMITER is None:
        _SPANNER_LIMITER = anyio.CapacityLimiter(_read_limit())
    return _SPANNER_LIMITER


def reset_limiter_for_tests() -> None:
    """Drop the cached limiter so the next ``to_thread`` rebuilds it.

    Tests that set ``SPANNER_THREADPOOL_LIMIT`` after import need this to
    pick up the new value. Production code never calls it.
    """
    global _SPANNER_LIMITER
    _SPANNER_LIMITER = None


# ---------------------------------------------------------------------------
# The seam itself.
# ---------------------------------------------------------------------------

async def to_thread(
    fn: Callable[..., T],
    *args: Any,
    op_name: str = "spanner.call",
    read_only: bool = False,
    attributes: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> T:
    """Run a synchronous Spanner call in the AnyIO threadpool.

    Wrap every ``database.snapshot()`` / ``database.batch()`` /
    ``database.run_in_transaction()`` invocation in this helper so the
    event loop is not blocked while the gRPC call is in flight.

    Combine with ``DeadlineGuard.run`` (or ``asyncio.wait_for``) at the
    call site for a client-side deadline AND pass a server-side
    deadline through to the underlying call (``snapshot.execute_sql(...,
    timeout=budget)`` / ``database.run_in_transaction(..., timeout_secs
    =budget)``) so abandon-on-cancel reads and abandon-off writes are
    both bounded.

    Parameters
    ----------
    op_name
        OpenTelemetry span name. Defaults to ``"spanner.call"``; pass a
        more specific value (``"spanner.execute_sql"``,
        ``"spanner.batch.insert_or_update"``, ...) at call sites where
        the granularity matters for trace navigation. The kwarg is
        consumed by the seam and never forwarded to ``fn``.
    read_only
        When ``True`` the seam passes ``abandon_on_cancel=True`` to
        anyio so a cancelled awaiter doesn't leak a thread holding a
        session + limiter slot. Use for snapshot reads that have a
        server-side ``timeout=`` bound. Writes/transactions must keep
        the default (``False``) — see the module docstring.
    attributes
        Optional OTel span attributes (``project_id``, ``instance_id``,
        ``database_id``, ``statement_kind``, ``row_count``, ...). The
        seam adds them to the span when otel is installed. Cheap when
        otel is absent (the dict is dropped).
    """
    runner = lambda: fn(*args, **kwargs)
    limiter = _get_limiter()

    if _tracer is None:
        return await anyio.to_thread.run_sync(
            runner, abandon_on_cancel=read_only, limiter=limiter,
        )

    # Span covers the entire await — threadpool acquisition + gRPC
    # round-trip + result return. The span is closed in __aexit__ even
    # if the inner call raises, so timeouts and gRPC errors carry the
    # span context for correlation.
    with _tracer.start_as_current_span(op_name) as span:  # pragma: no cover
        if attributes:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:
                    # OTel rejects unsupported attribute types; never let
                    # observability take down a Spanner call.
                    pass
        try:
            result = await anyio.to_thread.run_sync(
                runner, abandon_on_cancel=read_only, limiter=limiter,
            )
            span.set_attribute("spanner.outcome", "ok")
            return result
        except BaseException as exc:
            span.set_attribute("spanner.outcome", "error")
            span.set_attribute("spanner.error.type", type(exc).__name__)
            span.record_exception(exc)
            raise
