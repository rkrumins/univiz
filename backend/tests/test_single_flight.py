"""Unit tests for backend.app.common.single_flight.

These pin the invariants that make single-flight safe to put on a hot
path: leader executes exactly once, all followers get the same
result, every exit path clears the in-flight dict, and TTL eviction
keeps memory bounded when something goes wrong.
"""
import asyncio
import pytest

from backend.app.common.single_flight import (
    SingleFlight,
    normalised_principal,
)


async def test_runs_fn_once_under_concurrent_callers():
    """200 concurrent calls with the same key → fn runs exactly once.
    This is the whole point of the primitive; if it fails here, the
    backend will still see the thundering herd we built this to stop.
    """
    sf = SingleFlight()
    call_count = 0

    async def expensive():
        nonlocal call_count
        call_count += 1
        # Yield once so concurrent callers actually join the future;
        # without this the leader could complete synchronously before
        # any follower gets to register.
        await asyncio.sleep(0)
        return "ok"

    results = await asyncio.gather(*[sf.run("k", expensive) for _ in range(200)])

    assert call_count == 1, f"expected single execution, got {call_count}"
    assert results == ["ok"] * 200
    assert sf.in_flight_count() == 0, "key should be popped after completion"


async def test_distinct_keys_run_independently():
    """Different keys don't dedup against each other — the whole point
    of keys is to scope the dedup to logically-identical work."""
    sf = SingleFlight()
    counts = {"a": 0, "b": 0}

    async def runner(label: str):
        counts[label] += 1
        await asyncio.sleep(0)
        return label

    out = await asyncio.gather(
        sf.run("a", lambda: runner("a")),
        sf.run("a", lambda: runner("a")),
        sf.run("b", lambda: runner("b")),
        sf.run("b", lambda: runner("b")),
    )
    assert out == ["a", "a", "b", "b"]
    assert counts == {"a": 1, "b": 1}


async def test_leader_exception_propagates_to_followers():
    """If the leader's work raises, every follower must see the same
    exception. Otherwise followers would hang forever on a future
    that never resolved."""
    sf = SingleFlight()

    class Boom(RuntimeError):
        pass

    async def explosive():
        await asyncio.sleep(0)
        raise Boom("kaboom")

    # Kick off followers and the leader together. We need to gather
    # exceptions individually because asyncio.gather with
    # return_exceptions=False would short-circuit on the first.
    tasks = [asyncio.create_task(sf.run("k", explosive)) for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(r, Boom) for r in results), f"got {results!r}"
    assert sf.in_flight_count() == 0, "key must be cleared after exception"


async def test_key_cleared_after_exception_allows_retry():
    """Following an exception, the next call for the same key must
    be able to enter as a fresh leader (no permanent 'poisoned'
    state). Without this, a transient downstream failure would
    permanently break the endpoint for the duration of the worker."""
    sf = SingleFlight()
    attempt = 0

    async def maybe_fails():
        nonlocal attempt
        attempt += 1
        await asyncio.sleep(0)
        if attempt == 1:
            raise RuntimeError("first call fails")
        return "second call works"

    with pytest.raises(RuntimeError):
        await sf.run("k", maybe_fails)

    # Second attempt should succeed cleanly.
    assert await sf.run("k", maybe_fails) == "second call works"
    assert attempt == 2


async def test_leader_cancellation_wakes_followers():
    """If the leader is cancelled mid-flight, followers must see the
    cancellation rather than hanging on a never-resolved future.
    asyncio.CancelledError is the contractually-correct signal —
    followers can handle it the same way they'd handle a direct
    cancel on their own task."""
    sf = SingleFlight()
    leader_started = asyncio.Event()
    proceed = asyncio.Event()

    async def hangs():
        leader_started.set()
        try:
            await proceed.wait()
        except asyncio.CancelledError:
            raise
        return "should not reach"

    leader_task = asyncio.create_task(sf.run("k", hangs))
    await leader_started.wait()

    # Now there's a follower joining.
    follower_task = asyncio.create_task(sf.run("k", hangs))
    # Give the follower a moment to actually attach to the future.
    await asyncio.sleep(0)

    leader_task.cancel()
    # Both should raise CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await leader_task
    with pytest.raises(asyncio.CancelledError):
        await follower_task

    assert sf.in_flight_count() == 0, "key must be cleared after cancellation"


async def test_ttl_eviction_lets_a_new_leader_take_over():
    """A leader that somehow disappears without going through the
    ``finally`` (impossible in this codebase but possible in adversarial
    test conditions) leaves an orphan entry. After ``ttl_seconds`` the
    next caller must enter as a fresh leader, not block forever.

    We force the leak by directly mutating the internal dict — same
    end-state as a coroutine that GC'd before reaching its finally.
    """
    sf = SingleFlight(ttl_seconds=0.05)
    loop = asyncio.get_running_loop()

    # Inject an orphan with an already-past deadline.
    orphan = loop.create_future()
    sf._in_flight["k"] = (orphan, loop.time() - 1.0)

    async def fresh():
        return "took over"

    # Without TTL eviction, this would await on the orphan forever.
    result = await asyncio.wait_for(sf.run("k", fresh), timeout=1.0)
    assert result == "took over"
    assert sf.in_flight_count() == 0


async def test_in_flight_count_during_execution():
    """Sanity check that in_flight_count is observable while a leader
    is running. Used by tests and (potentially) the metrics endpoint."""
    sf = SingleFlight()
    proceed = asyncio.Event()

    async def slow():
        await proceed.wait()
        return None

    task = asyncio.create_task(sf.run("k", slow))
    # Wait a tick so the leader has installed the key.
    await asyncio.sleep(0)
    assert sf.in_flight_count() == 1
    proceed.set()
    await task
    assert sf.in_flight_count() == 0


# ── normalised_principal helper ─────────────────────────────────────


def test_normalised_principal_with_user_id():
    assert normalised_principal("usr_abc") == "usr_abc"


def test_normalised_principal_anon_for_none():
    """Anonymous callers must share a key so they get deduped together
    — they see the same data, so deduping them is the whole point."""
    assert normalised_principal(None) == "anon"
    assert normalised_principal("") == "anon"
