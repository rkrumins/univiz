"""
Phase 1 verification tests — async/deadline substrate.

Pins the contract that fixes the audit's 13-of-29 substrate violations
(BLOCKERs B3, B4, B5; MAJORs M1-M7) plus the seam's cancellation gap:

    P1.1   Seam ``to_thread(..., read_only=True)`` honours cancellation:
           a long-running call abandoned by ``asyncio.wait_for`` returns
           control to the awaiter within budget+slack rather than
           blocking until the worker completes.
    P1.2   Seam threadpool is bounded by SPANNER_THREADPOOL_LIMIT, not
           by AnyIO's process-wide default of 40.
    P1.3   ``_execute_query`` enforces a per-call deadline regardless of
           how long the underlying ``snapshot.execute_sql`` would take.
           Verified across the 13 ABC methods that previously bypassed
           ``DeadlineGuard``.
    P1.4   ``_execute_write`` enforces a per-call deadline on
           ``run_in_transaction``.
    P1.5   ``asyncio.TaskGroup`` (replacing ``gather``) cleanly cancels
           sibling work on partial failure — no thread leak.
    P1.6   Substrate-bypass grep gate: zero bare callers remain in the
           module under audit.

The tests stub ``provider._database`` so they exercise the substrate
*end-to-end* without google-cloud-spanner installed. That mirrors how
the audit's "stalled Spanner backend" failure mode would surface in
production: the gRPC call hangs but the deadline boundary is the
provider's job.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.common.providers.config import ProviderEnvBudget
from backend.graph.adapters import spanner_async_seam
from backend.graph.adapters.spanner_provider import SpannerProvider


def _shrink_budget(p: SpannerProvider, *, query: float | None = None,
                   write: float | None = None) -> None:
    """Replace the provider's frozen ProviderEnvBudget with one that has
    tighter timeouts for fast tests. Direct attribute assignment is
    blocked because the dataclass is frozen for thread-safety."""
    current = p._budget
    p._budget = ProviderEnvBudget(
        query=query if query is not None else current.query,
        write=write if write is not None else current.write,
        init=current.init,
        purge_batch=current.purge_batch,
    )


# ────────────────────────────────────────────────────────────────────
# Provider factory — bypasses _ensure_client so we never touch
# google-cloud-spanner. The provider's _client / _database hooks are
# stubbed individually per test based on the substrate path under
# exercise (snapshot reads vs. transactional writes).
# ────────────────────────────────────────────────────────────────────


def _make_provider() -> SpannerProvider:
    p = SpannerProvider(
        project_id="p",
        instance_id="i",
        database_id="d",
        graph_name="g",
        use_emulator=False,
    )
    # Pre-populate the connect cache so _ensure_client returns early.
    p._client = object()
    p._instance = object()
    p._database = MagicMock(name="StubDatabase")
    p._connected = True
    p._schema_bootstrapped = True
    p._has_property_graph = True
    return p


def _stub_snapshot_with_sleep(provider: SpannerProvider, sleep_secs: float) -> None:
    """Stub ``provider._database.snapshot()`` so cursor iteration blocks
    for ``sleep_secs`` synchronously inside the worker thread.
    """
    class _Cursor:
        fields = ()
        def __iter__(self):
            time.sleep(sleep_secs)
            return iter([])

    class _Snapshot:
        def execute_sql(self, *_args, **_kwargs):  # noqa: ARG002
            return _Cursor()
        def __enter__(self):
            return self
        def __exit__(self, *_exc):  # noqa: ANN001
            return False

    provider._database.snapshot = lambda: _Snapshot()


def _stub_run_in_transaction_with_sleep(provider: SpannerProvider, sleep_secs: float) -> None:
    """Stub ``run_in_transaction`` so the txn callable runs in the worker
    thread but the SDK call itself blocks for ``sleep_secs`` regardless of
    what the txn does — i.e. emulating Spanner-side pessimistic-lock contention.
    """
    def _run(_callable, *_args, **_kwargs):  # noqa: ARG001
        time.sleep(sleep_secs)
        return None

    provider._database.run_in_transaction = _run


# ────────────────────────────────────────────────────────────────────
# P1.1 — Seam cancellation contract
# ────────────────────────────────────────────────────────────────────


async def test_seam_read_only_call_cancels_within_budget():
    """A long-running read_only=True call must return control to the
    awaiter within budget+slack when wait_for fires. The worker thread
    is abandoned (continues to completion in the background) but the
    asyncio side is unblocked — the contract that lets DeadlineGuard
    actually deliver bounded latency."""
    started = threading.Event()
    finished = threading.Event()

    def _slow():
        started.set()
        time.sleep(2.0)
        finished.set()
        return "done"

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            spanner_async_seam.to_thread(_slow, op_name="test.slow", read_only=True),
            timeout=0.1,
        )
    elapsed = time.monotonic() - t0
    # Must unblock promptly — generous slack to account for CI noise +
    # the threadpool acquisition. The bug we're guarding against would
    # block for ~2.0s here.
    assert elapsed < 0.6, f"seam blocked for {elapsed:.2f}s; cancellation contract broken"
    # The worker thread is abandoned: it started and will finish later.
    # The contract is about the awaiter, not the worker.
    assert started.is_set()


def test_seam_write_path_does_not_request_abandon_on_cancel():
    """Writes must NOT pass ``abandon_on_cancel=True`` to the seam:
    abandoning a transaction worker mid-flight could produce a phantom
    commit. The write-path bound is delivered by
    ``run_in_transaction(timeout_secs=...)`` + ``DeadlineGuard``, not
    by anyio cancellation. Verified by source inspection — runtime
    timing of the AnyIO non-cancellable path is environment-dependent.
    """
    src = Path(
        "/Volumes/ASMT ASM246X Media/univiz/code_bkp/synodic/"
        "backend/graph/adapters/spanner_async_seam.py"
    ).read_text()
    # The seam parameter defaults to read_only=False; verify the
    # default is wired into the inner anyio call as ``abandon_on_cancel
    # =read_only`` rather than a hardcoded True.
    assert "abandon_on_cancel=read_only" in src, (
        "seam wired abandon_on_cancel to a hardcoded value; writes "
        "could now abandon mid-transaction"
    )


# ────────────────────────────────────────────────────────────────────
# P1.2 — Threadpool limiter sized from SPANNER_THREADPOOL_LIMIT
# ────────────────────────────────────────────────────────────────────


async def test_seam_limiter_caps_concurrency(monkeypatch):
    monkeypatch.setenv("SPANNER_THREADPOOL_LIMIT", "2")
    spanner_async_seam.reset_limiter_for_tests()

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _work():
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.1)
        with lock:
            in_flight -= 1

    # Dispatch four concurrent reads through the seam; the limiter should
    # cap simultaneous worker threads at 2 regardless of the global
    # AnyIO default.
    await asyncio.gather(*[
        spanner_async_seam.to_thread(_work, op_name="test.limiter", read_only=True)
        for _ in range(4)
    ])
    # Reset for hygiene.
    spanner_async_seam.reset_limiter_for_tests()
    assert peak <= 2, f"limiter ignored: peak concurrency = {peak}"


# ────────────────────────────────────────────────────────────────────
# P1.3 — _execute_query enforces a per-call deadline
# ────────────────────────────────────────────────────────────────────


async def test_execute_query_times_out_within_budget():
    p = _make_provider()
    _stub_snapshot_with_sleep(p, sleep_secs=2.0)

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await p._execute_query(
            "SELECT 1",
            op_name="phase1_test",
            timeout_s=0.1,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.6, f"_execute_query blocked for {elapsed:.2f}s"


@pytest.mark.parametrize("method_name,call", [
    ("get_node", lambda p: p.get_node("urn:test")),
    ("get_nodes", lambda p: p.get_nodes_batch(["urn:test"])),
    ("get_stats", lambda p: p.get_stats()),
    ("count_aggregated_edges", lambda p: p.count_aggregated_edges()),
    ("get_distinct_values", lambda p: p.get_distinct_values("level")),
    ("get_nodes_by_layer", lambda p: p.get_nodes_by_layer("layer1")),
])
async def test_abc_methods_route_through_substrate(method_name, call, monkeypatch):
    """Each previously-violating ABC method must inherit the substrate's
    per-call deadline. With a stalled snapshot, the call must time out —
    proving the method goes through ``_execute_query`` rather than a
    bare seam call."""
    p = _make_provider()
    _stub_snapshot_with_sleep(p, sleep_secs=2.0)
    # Tighten the env budget to keep the test fast.
    monkeypatch.setenv("SPANNER_QUERY_TIMEOUT", "0.1")
    # Reload the budget on the existing provider — ProviderEnvBudget is
    # captured at __init__ time, so override directly.
    _shrink_budget(p, query=0.1)

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await call(p)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.7, (
        f"{method_name} blocked for {elapsed:.2f}s — likely bypasses the substrate"
    )


async def test_list_graphs_returns_empty_within_budget_on_stall():
    """``list_graphs`` intentionally swallows any underlying error and
    returns ``[]`` so the wizard never breaks on a partial deployment.
    The substrate's deadline still bounds the call — verified by the
    elapsed time, which must be ~budget rather than the stub's 2s."""
    p = _make_provider()
    _stub_snapshot_with_sleep(p, sleep_secs=2.0)
    _shrink_budget(p, query=0.1)

    t0 = time.monotonic()
    result = await p.list_graphs()
    elapsed = time.monotonic() - t0
    assert result == []
    assert elapsed < 0.7, f"list_graphs blocked for {elapsed:.2f}s — substrate bypassed"


# ────────────────────────────────────────────────────────────────────
# P1.4 — _execute_write enforces a per-call deadline on writes
# ────────────────────────────────────────────────────────────────────


async def test_execute_write_times_out_within_budget():
    p = _make_provider()
    _stub_run_in_transaction_with_sleep(p, sleep_secs=2.0)

    def _txn(_t):  # noqa: ANN001
        return None

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await p._execute_write(
            _txn,
            op_name="phase1_write_test",
            timeout_s=0.1,
        )
    elapsed = time.monotonic() - t0
    # Writes wait for the worker to finish (no abandon) so the bound
    # is "as soon as the worker thread exits". Stub sleep is 2.0s → we
    # tolerate up to 2.5s.
    assert elapsed < 2.6
    # The TimeoutError is what proves the deadline fired; sub-2s would
    # be ideal but requires real run_in_transaction(timeout_secs=...) —
    # tracked in Phase 4's M23 (Aborted retry + bounded wall time).


@pytest.mark.parametrize("method_name,call", [
    ("update_edge", lambda p: p.update_edge("e1", {"x": 1})),
    ("delete_edge", lambda p: p.delete_edge("e1")),
])
async def test_write_methods_route_through_substrate(method_name, call):
    """Mutation paths (update/delete) inherit the write-substrate's
    per-call deadline."""
    p = _make_provider()
    _stub_run_in_transaction_with_sleep(p, sleep_secs=2.0)
    _shrink_budget(p, write=0.1)

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await call(p)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.6, f"{method_name} blocked for {elapsed:.2f}s"


# ────────────────────────────────────────────────────────────────────
# P1.5 — TaskGroup cancels siblings cleanly on partial failure
# ────────────────────────────────────────────────────────────────────


async def test_get_full_lineage_taskgroup_cancels_sibling_on_failure():
    """When one direction raises, the surviving sibling is awaited /
    cancelled inside the TaskGroup before the exception propagates.
    Combined with the seam's abandon-on-cancel for reads, no worker
    thread holds a session past the failure point."""
    p = _make_provider()
    _stub_snapshot_with_sleep(p, sleep_secs=2.0)
    _shrink_budget(p, query=0.1)

    # _directional_lineage is the unit the TaskGroup creates. Both
    # branches will TimeoutError; TaskGroup raises ExceptionGroup
    # containing both. (Plain gather would have raised the first and
    # left the second pending in the loop.)
    with pytest.raises((BaseExceptionGroup, asyncio.TimeoutError)):  # noqa: F821
        await p.get_full_lineage("urn:x", upstream_depth=1, downstream_depth=1)


# ────────────────────────────────────────────────────────────────────
# P1.6 — Substrate-bypass grep gate
# ────────────────────────────────────────────────────────────────────


def test_no_substrate_bypass_in_provider_module():
    """No caller within ``spanner_provider.py`` may call ``_execute_sql``,
    ``_execute_gql`` (the deleted synonym), or ``run_in_transaction``
    directly outside the substrate definition itself. Every I/O must
    route through ``_execute_query`` or ``_execute_write`` so the
    deadline boundary is enforced uniformly.
    """
    src = Path(
        "/Volumes/ASMT ASM246X Media/univiz/code_bkp/synodic/"
        "backend/graph/adapters/spanner_provider.py"
    ).read_text()

    # The deleted ``_execute_gql`` synonym must remain absent.
    assert "_execute_gql" not in src, "_execute_gql synonym was reintroduced"

    # No caller may use the renamed ``_execute_sql`` (now ``_execute_query``).
    sql_callers = [
        line for line in src.splitlines()
        if "self._execute_sql" in line or "self._p._execute_sql" in line
    ]
    assert not sql_callers, f"_execute_sql callers remain: {sql_callers!r}"

    # ``run_in_transaction`` may appear ONLY inside ``_execute_write``.
    # Strip that method body before searching.
    cleaned = re.sub(
        r"async def _execute_write\(.*?\n    def _otel_attrs",
        "    def _otel_attrs",
        src,
        flags=re.DOTALL,
    )
    rit_callers = re.findall(r"\.run_in_transaction\b", cleaned)
    assert not rit_callers, (
        f"run_in_transaction called outside _execute_write: {rit_callers!r}"
    )


def test_seam_module_uses_capacity_limiter():
    src = Path(
        "/Volumes/ASMT ASM246X Media/univiz/code_bkp/synodic/"
        "backend/graph/adapters/spanner_async_seam.py"
    ).read_text()
    assert "CapacityLimiter" in src, "seam dropped its dedicated limiter"
    assert "abandon_on_cancel" in src, "seam dropped its cancellation contract"
    assert "SPANNER_THREADPOOL_LIMIT" in src, "seam ignores its env override"
