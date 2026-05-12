"""Shared adapter primitives for bulkheading external systems.

Any outbound-network dependency (graph provider, HTTP client, vector DB, etc.)
should wrap its concrete adapter in :class:`CircuitBreakerProxy` so that a
failing downstream cannot cascade into the web tier or starve the event loop.

Redis clients should be wrapped in :class:`TimeoutRedis` so that every async
call and pipeline execution has an ``asyncio.wait_for()`` deadline.
"""

from .circuit import (
    CircuitBreakerProxy,
    ProviderUnavailable,
    ProviderBusy,
    _AsyncCircuitBreaker as AsyncCircuitBreaker,
    _BreakerOpenError as BreakerOpenError,
    BreakerState,
)
from .timeout_redis import TimeoutRedis

__all__ = [
    "CircuitBreakerProxy",
    "ProviderUnavailable",
    "ProviderBusy",
    "AsyncCircuitBreaker",
    "BreakerOpenError",
    "BreakerState",
    "TimeoutRedis",
]
