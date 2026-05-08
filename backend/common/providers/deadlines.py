"""Deadline-aware async helper used across provider adapters.

The ABC requires every provider I/O call to be bounded by a per-operation
deadline. DeadlineGuard centralises the asyncio.wait_for boilerplate so
each call site emits the same structured log shape on timeout, making
incident triage uniform across providers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DeadlineGuard:
    """Wraps coroutines in asyncio.wait_for with consistent logging.

    Usage::

        guard = DeadlineGuard(provider_name="spanner")
        result = await guard.run(
            self._execute_sql(sql, params),
            op_name="get_node",
            timeout_s=5.0,
        )
    """

    def __init__(self, *, provider_name: str) -> None:
        self._provider_name = provider_name

    async def run(
        self,
        coro: Awaitable[T],
        *,
        op_name: str,
        timeout_s: float,
    ) -> T:
        start = time.monotonic()
        try:
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "%s.%s timed out after %dms (budget=%.1fs)",
                self._provider_name, op_name, elapsed_ms, timeout_s,
                extra={
                    "provider": self._provider_name,
                    "op": op_name,
                    "elapsed_ms": elapsed_ms,
                    "deadline_s": timeout_s,
                    "outcome": "timeout",
                },
            )
            raise

    @asynccontextmanager
    async def block(self, *, op_name: str, timeout_s: float):
        """Context-manager form for a code block that uses sub-tasks.

        Inside the block, ``deadline_remaining_s()`` lets sub-operations
        use the remaining budget. The guard does NOT actually cancel the
        block on timeout (the caller must check); the helper exists so
        BFS-style orchestrators can carve a parent budget into per-hop
        sub-budgets.
        """
        deadline = time.monotonic() + timeout_s

        def _remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        # Stash on a small object the caller can inspect.
        ctx = _DeadlineContext(remaining=_remaining)
        try:
            yield ctx
        finally:
            elapsed_ms = int((time.monotonic() - (deadline - timeout_s)) * 1000)
            if _remaining() == 0.0:
                logger.warning(
                    "%s.%s exhausted budget (%dms / %.1fs)",
                    self._provider_name, op_name, elapsed_ms, timeout_s,
                    extra={
                        "provider": self._provider_name,
                        "op": op_name,
                        "elapsed_ms": elapsed_ms,
                        "deadline_s": timeout_s,
                        "outcome": "exhausted",
                    },
                )


class _DeadlineContext:
    """Object yielded from ``DeadlineGuard.block``; exposes remaining budget."""

    __slots__ = ("_remaining_fn",)

    def __init__(self, remaining: Callable[[], float]) -> None:
        self._remaining_fn = remaining

    def remaining_s(self) -> float:
        """Seconds left in the parent block's budget. May be 0.0."""
        return self._remaining_fn()
