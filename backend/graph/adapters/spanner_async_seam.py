"""Async/sync seam for the Spanner client.

The official ``google-cloud-spanner`` high-level API
(``Client``/``Database``/``Snapshot``/``Batch``) is synchronous. The
async low-level ``SpannerAsyncClient`` exists at the gRPC layer but
lacks session-pooling, transparent streaming-result resumption, and
``Aborted``-retry ergonomics. This module is the single place that
bridges the two worlds.

When Google ships a high-level async API, this is the only file that
needs to change.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

import anyio

T = TypeVar("T")


async def to_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous Spanner call in the AnyIO threadpool.

    Wrap every ``database.snapshot()``/``database.batch()``/
    ``database.run_in_transaction()`` invocation in this helper so the
    event loop is not blocked while the gRPC call is in flight.

    Combine with ``asyncio.wait_for(...)`` at the call site for a
    client-side deadline; the underlying gRPC channel also takes a
    server-side deadline via ``request_options.request_tag`` (TBD per
    call site).
    """
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
