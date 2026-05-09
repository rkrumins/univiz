"""Async/sync seam for the Spanner client.

The official ``google-cloud-spanner`` high-level API
(``Client``/``Database``/``Snapshot``/``Batch``) is synchronous. The
async low-level ``SpannerAsyncClient`` exists at the gRPC layer but
lacks session-pooling, transparent streaming-result resumption, and
``Aborted``-retry ergonomics. This module is the single place that
bridges the two worlds.

When Google ships a high-level async API, this is the only file that
needs to change.

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
from typing import Any, Callable, TypeVar

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


async def to_thread(
    fn: Callable[..., T],
    *args: Any,
    op_name: str = "spanner.call",
    **kwargs: Any,
) -> T:
    """Run a synchronous Spanner call in the AnyIO threadpool.

    Wrap every ``database.snapshot()``/``database.batch()``/
    ``database.run_in_transaction()`` invocation in this helper so the
    event loop is not blocked while the gRPC call is in flight.

    Combine with ``asyncio.wait_for(...)`` at the call site for a
    client-side deadline; the underlying gRPC channel also takes a
    server-side deadline via ``request_options.request_tag``.

    Parameters
    ----------
    op_name
        OpenTelemetry span name. Defaults to ``"spanner.call"``; pass a
        more specific value (``"spanner.execute_sql"``,
        ``"spanner.batch.insert_or_update"``, ...) at call sites where
        the granularity matters for trace navigation. The kwarg is
        consumed by the seam and never forwarded to ``fn``.
    """
    runner = lambda: fn(*args, **kwargs)
    if _tracer is None:
        return await anyio.to_thread.run_sync(runner)

    # Span covers the entire await — threadpool acquisition + gRPC
    # round-trip + result return. The span is closed in __aexit__ even
    # if the inner call raises, so timeouts and gRPC errors carry the
    # span context for correlation.
    with _tracer.start_as_current_span(op_name) as span:  # pragma: no cover
        try:
            result = await anyio.to_thread.run_sync(runner)
            span.set_attribute("spanner.outcome", "ok")
            return result
        except BaseException as exc:
            span.set_attribute("spanner.outcome", "error")
            span.set_attribute("spanner.error.type", type(exc).__name__)
            span.record_exception(exc)
            raise
