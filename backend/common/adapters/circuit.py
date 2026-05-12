"""Per-instance circuit breaker for external-adapter bulkheading.

Wraps any object (typically a :class:`GraphDataProvider` implementation) in a
transparent async proxy whose method calls route through a minimal async
state machine (CLOSED → OPEN → HALF_OPEN → …). When the downstream fails
past the configured threshold, the breaker opens; subsequent calls then
fail fast with :class:`ProviderUnavailable` — no socket I/O, no coroutine
scheduling, no event-loop stall. Consumers (e.g. FastAPI exception
handlers) map the exception to an HTTP 503 with a ``Retry-After`` header.

Why we rolled our own state machine instead of using `pybreaker.call_async`:
`pybreaker` 1.x ships an async call helper that depends on ``tornado.gen``
— when Tornado is not installed the import silently binds ``gen`` to
``None`` and ``call_async`` raises ``NameError`` at first use. Rather than
pin a hidden Tornado dep, we keep the ~60 LOC state machine below and own
the invariants. The state transitions match pybreaker's semantics (and
mirror Release It!'s canonical description) so operators can reason about
it from existing literature.

Design notes
------------
* The breaker is **per target instance**, never global: one sick FalkorDB
  must not trip the breaker for a healthy Neo4j.
* Sync attributes, sync methods, dunder methods, and members listed in
  :data:`_UNWRAPPED_METHODS` pass through untouched. Only ``async def``
  methods are guarded.
* Only "network-class" errors count toward the failure budget. Logical
  errors (``ValueError``, ``KeyError``, ``ProviderConfigurationError``,
  etc.) are re-raised verbatim and do not affect breaker state — they
  indicate caller bugs, not downstream failure.
* Network errors are sanitized into :class:`ProviderUnavailable` at the
  proxy boundary so upstream code never has to reason about
  :mod:`redis.exceptions` vs builtin :class:`OSError` vs :mod:`httpx`
  exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Names that must NEVER be breaker-wrapped. ``close`` must run even when the
# breaker is open so pool resources can be freed during eviction; ``name`` is
# a property used for logging and must always be readable.
_UNWRAPPED_METHODS: frozenset[str] = frozenset({"close", "name"})


# Logical errors raised by adapter code — not downstream failures. Extended
# dynamically below with domain-specific logical exceptions when importable.
_DEFAULT_IGNORED_EXCEPTIONS: list[type[BaseException]] = [
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    NotImplementedError,
]

try:
    from backend.common.interfaces.provider import ProviderConfigurationError

    _DEFAULT_IGNORED_EXCEPTIONS.append(ProviderConfigurationError)
except Exception:  # pragma: no cover - import-time only
    pass

def register_logical_exception(exc_type: type[BaseException]) -> None:
    """Register a domain exception as a logical/control-flow signal.

    Exceptions registered here are re-raised untouched by guarded proxies —
    they are not counted as breaker failures and are not wrapped as
    :class:`ProviderUnavailable`. Use for cooperative-cancel signals and
    other control-flow exceptions that happen to inherit from ``Exception``.

    Idempotent. Must be called *before* the provider's
    :class:`CircuitBreakerProxy` is constructed; proxies snapshot the
    ignored set at ``__init__`` time. Calling from the defining module's
    top level (after the class is defined) is the supported pattern —
    that avoids the circular-import trap of having ``circuit.py`` reach
    sideways into application packages at its own import time.
    """
    if exc_type not in _DEFAULT_IGNORED_EXCEPTIONS:
        _DEFAULT_IGNORED_EXCEPTIONS.append(exc_type)


def _default_network_exceptions() -> tuple[type[BaseException], ...]:
    """Build the tuple of exception classes treated as "downstream is sick"."""
    errors: list[type[BaseException]] = [
        ConnectionError,
        OSError,
        TimeoutError,
        asyncio.TimeoutError,
    ]
    try:
        from redis.exceptions import ConnectionError as _RedisConnectionError
        from redis.exceptions import TimeoutError as _RedisTimeoutError

        errors.extend([_RedisConnectionError, _RedisTimeoutError])
    except ImportError:  # pragma: no cover
        pass
    try:
        import httpx

        errors.extend(
            [httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.ReadError]
        )
    except ImportError:  # pragma: no cover
        pass
    return tuple(errors)


_NETWORK_EXCEPTIONS = _default_network_exceptions()


class BreakerState(str, Enum):
    """Classic circuit-breaker states. String values chosen to match
    :mod:`pybreaker` (``"closed"``, ``"open"``, ``"half-open"``) so any
    external tooling keyed on those strings keeps working."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class ProviderUnavailable(Exception):
    """Sanitized exception raised when a guarded adapter is unreachable.

    Carries a ``retry_after_seconds`` hint suitable for the HTTP
    ``Retry-After`` header. The low-level :mod:`redis` / :mod:`httpx` /
    :class:`OSError` cause is available via ``__cause__`` but never leaks
    beyond the proxy boundary in ``str(exc)``.
    """

    def __init__(
        self,
        provider_name: str,
        reason: str,
        retry_after_seconds: int = 30,
    ) -> None:
        self.provider_name = provider_name
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Provider '{provider_name}' unavailable: {reason}")


class ProviderBusy(ProviderUnavailable):
    """Phase 2 — flow-control signal, NOT a failure.

    Raised by a provider when its observed write-side latency has crept
    above the latency-quiesce threshold. Semantically distinct from
    ``ProviderUnavailable``:

    - ``ProviderUnavailable`` = the provider is broken (network down,
      breaker open, credentials wrong). Worker counts this against the
      retry budget; on exhaustion the job moves to ``failed``.
    - ``ProviderBusy`` = the provider is healthy but overloaded right
      now. Worker should **park the job** (preserve cursor / phase, do
      NOT increment retry_count, do NOT mark failed) and re-dispatch
      after the ``retry_after_seconds`` cooldown.

    The two share the same surface (``ProviderUnavailable`` ancestor)
    so existing HTTP handlers / metric emitters that filter on the
    parent type continue to work. New worker code can isinstance-check
    for ``ProviderBusy`` specifically to apply the park-and-resume
    treatment.
    """

    def __init__(
        self,
        provider_name: str,
        reason: str,
        retry_after_seconds: int = 30,
    ) -> None:
        super().__init__(provider_name, reason, retry_after_seconds)


class _AsyncCircuitBreaker:
    """Minimal async-safe circuit-breaker state machine.

    Single instance owns state + a lock. Transitions are protected by the
    lock so concurrent callers cannot race into both OPEN and HALF_OPEN at
    once. The critical section is tiny — no I/O — so contention is
    negligible.
    """

    def __init__(
        self,
        *,
        name: str,
        fail_max: int,
        reset_timeout: int,
    ) -> None:
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._state = BreakerState.CLOSED
        self._fail_counter = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def current_state(self) -> str:
        """String form of the state (matches pybreaker's contract).

        Note: this property reads ``_state`` and ``_opened_at`` without the
        lock — it is intended for diagnostic / log-line use. Code that needs
        a consistent snapshot alongside a state mutation must obtain it from
        the value returned by ``_record_failure`` / ``_record_success``,
        which sample inside the critical section.
        """
        if self._state == BreakerState.OPEN:
            if (
                self._opened_at is not None
                and time.monotonic() - self._opened_at >= self.reset_timeout
            ):
                return BreakerState.HALF_OPEN.value
        return self._state.value

    @property
    def fail_counter(self) -> int:
        return self._fail_counter

    def open(self) -> None:
        """Manually trip the breaker (used by tests + diagnostics)."""
        self._state = BreakerState.OPEN
        self._opened_at = time.monotonic()
        if self._fail_counter < self.fail_max:
            self._fail_counter = self.fail_max

    async def _acquire_call_slot(self) -> None:
        """Raise if the breaker will not permit a call right now.

        In OPEN: raise if the reset_timeout hasn't elapsed; otherwise
        transition to HALF_OPEN and allow this single call through.
        """
        async with self._lock:
            if self._state == BreakerState.OPEN:
                assert self._opened_at is not None
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self.reset_timeout:
                    raise _BreakerOpenError(retry_after_seconds=int(self.reset_timeout - elapsed))
                # Probe window has arrived — move to HALF_OPEN for exactly
                # one trial call.
                self._state = BreakerState.HALF_OPEN
                logger.info(
                    "Circuit breaker '%s' transition OPEN -> HALF_OPEN "
                    "(reset_timeout=%ds elapsed; probing downstream)",
                    self.name,
                    self.reset_timeout,
                )

    async def _record_success(self) -> tuple[str, int]:
        """Record a successful call. Returns (state_str, fail_counter) snapshot."""
        async with self._lock:
            self._fail_counter = 0
            self._state = BreakerState.CLOSED
            self._opened_at = None
            return self._state.value, self._fail_counter

    async def _record_failure(self) -> tuple[str, int]:
        """Record a failed call. Returns (state_str, fail_counter) snapshot
        captured inside the critical section so callers can log a value
        consistent with the transition that just occurred."""
        async with self._lock:
            if self._state == BreakerState.HALF_OPEN:
                # Probe failed — re-open immediately.
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                logger.info(
                    "Circuit breaker '%s' transition HALF_OPEN -> OPEN "
                    "(probe failed; reset_timeout=%ds)",
                    self.name,
                    self.reset_timeout,
                )
                return self._state.value, self._fail_counter
            self._fail_counter += 1
            if self._fail_counter >= self.fail_max:
                was_closed = self._state == BreakerState.CLOSED
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                if was_closed:
                    logger.info(
                        "Circuit breaker '%s' transition CLOSED -> OPEN "
                        "(fails=%d/%d; reset_timeout=%ds)",
                        self.name,
                        self._fail_counter,
                        self.fail_max,
                        self.reset_timeout,
                    )
            return self._state.value, self._fail_counter


class _BreakerOpenError(Exception):
    """Internal signal raised when the breaker refuses a call."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("circuit open")


class CircuitBreakerProxy:
    """Async proxy that routes every async-method call on ``target`` through a
    per-instance circuit breaker.

    Responsibility split: the proxy is a *state observer + fast-fail gate*.
    It classifies completed calls as healthy or sick, opens after the
    failure budget is exceeded, and short-circuits subsequent calls during
    the open window. It does **not** impose a deadline on the wrapped
    target. Per-operation deadlines are the wrapped target's responsibility
    because only the target knows the right granularity — a single query
    has a very different acceptable latency than a long-running batch
    method composed of many short, individually-deadlined operations. An
    outer proxy-level timeout would cancel healthy long-running orchestrations
    and falsely classify them as unhealthy, tripping the breaker on legitimate
    work.

    Parameters
    ----------
    target:
        The adapter instance to wrap (e.g. a :class:`GraphDataProvider`).
    name:
        Human-readable identifier used in breaker state and log lines.
    fail_max:
        Number of consecutive downstream failures before the breaker opens.
    reset_timeout:
        Seconds the breaker stays open before probing the downstream again
        (half-open state). Also surfaced as the ``Retry-After`` hint.
    extra_ignored_exceptions:
        Logical-error classes raised by *this* adapter that should not
        affect breaker state on top of the defaults.
    """

    def __init__(
        self,
        target: Any,
        name: str,
        *,
        fail_max: int = 5,
        reset_timeout: int = 30,
        extra_ignored_exceptions: Iterable[type[BaseException]] = (),
    ) -> None:
        self._target = target
        self._name = name
        self._ignored = tuple(_DEFAULT_IGNORED_EXCEPTIONS) + tuple(extra_ignored_exceptions)
        self._breaker = _AsyncCircuitBreaker(
            name=name,
            fail_max=fail_max,
            reset_timeout=reset_timeout,
        )

    # ── Introspection used by status endpoints + diagnostics ──────────

    @property
    def target(self) -> Any:
        """Access the wrapped adapter (bypasses the breaker)."""
        return self._target

    @property
    def breaker(self) -> _AsyncCircuitBreaker:
        return self._breaker

    @property
    def breaker_name(self) -> str:
        return self._name

    @property
    def breaker_state(self) -> str:
        return self._breaker.current_state

    def __repr__(self) -> str:
        return (
            f"CircuitBreakerProxy(name={self._name!r}, "
            f"state={self.breaker_state}, "
            f"fails={self._breaker.fail_counter}/{self._breaker.fail_max})"
        )

    # ── Call-forwarding machinery ─────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only invoked when the attribute is not found on self,
        # so our own _target/_name/_breaker are safe from recursion.
        attr = getattr(self._target, name)

        # Pass-through for non-callables, private names, and members that must
        # never be breaker-guarded (close, name property).
        if not callable(attr) or name.startswith("_") or name in _UNWRAPPED_METHODS:
            return attr

        # Sync methods are not network-bound (they're local transforms,
        # configuration setters, etc.) — do not wrap.
        if not asyncio.iscoroutinefunction(attr):
            return attr

        proxy = self

        async def breaker_guarded(*args: Any, **kwargs: Any) -> Any:
            # Fast path: try to acquire a call slot. Raises _BreakerOpenError
            # immediately (no coroutine scheduling, no I/O) if the breaker is
            # open and has not yet elapsed its reset_timeout.
            try:
                await proxy._breaker._acquire_call_slot()
            except _BreakerOpenError as exc:
                raise ProviderUnavailable(
                    provider_name=proxy._name,
                    reason=(
                        f"Circuit open; will probe downstream again in "
                        f"~{exc.retry_after_seconds}s"
                    ),
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None

            try:
                result = await attr(*args, **kwargs)
            except proxy._ignored:
                # Logical errors — re-raise untouched; breaker does not count these.
                raise
            except ProviderUnavailable as exc:
                # A nested adapter already sanitized this. Count it (downstream
                # is sick) and re-raise without double-wrapping.
                state_after, fails_after = await proxy._breaker._record_failure()
                logger.warning(
                    "Provider %s nested ProviderUnavailable on %s: %s (breaker=%s fails=%d/%d)",
                    proxy._name,
                    name,
                    exc,
                    state_after,
                    fails_after,
                    proxy._breaker.fail_max,
                )
                raise
            except _NETWORK_EXCEPTIONS as exc:
                state_after, fails_after = await proxy._breaker._record_failure()
                logger.warning(
                    "Provider %s network error on %s: %s=%s (breaker=%s fails=%d/%d)",
                    proxy._name,
                    name,
                    type(exc).__name__,
                    exc,
                    state_after,
                    fails_after,
                    proxy._breaker.fail_max,
                )
                raise ProviderUnavailable(
                    provider_name=proxy._name,
                    reason=f"{type(exc).__name__}: {exc}",
                    retry_after_seconds=int(proxy._breaker.reset_timeout),
                ) from exc
            except Exception as exc:
                # Any other Exception subclass — count it (downstream is
                # misbehaving in a way we don't have a specific class for)
                # and re-raise as ProviderUnavailable. Note: Exception (not
                # BaseException) so CancelledError/KeyboardInterrupt pass
                # through untouched.
                state_after, fails_after = await proxy._breaker._record_failure()
                logger.warning(
                    "Provider %s unexpected error on %s: %s=%s (breaker=%s fails=%d/%d)",
                    proxy._name,
                    name,
                    type(exc).__name__,
                    exc,
                    state_after,
                    fails_after,
                    proxy._breaker.fail_max,
                )
                raise ProviderUnavailable(
                    provider_name=proxy._name,
                    reason=f"{type(exc).__name__}: {exc}",
                    retry_after_seconds=int(proxy._breaker.reset_timeout),
                ) from exc

            else:
                await proxy._breaker._record_success()
                return result

        breaker_guarded.__name__ = f"breaker_guarded_{name}"
        breaker_guarded.__qualname__ = breaker_guarded.__name__
        return breaker_guarded
