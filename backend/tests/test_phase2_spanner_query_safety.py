"""
Phase 2 verification tests — query safety.

Pins the contract that closes audit BLOCKERs B6/B7 (graph_name DDL/GQL
injection) and MAJORs M8/M9 (unbounded UNNEST arrays + asymmetric
param_types_).

    P2.1   ``graph_name`` is regex-validated in __init__; malicious
           values raise ProviderConfigurationError before any I/O.
    P2.2   ``_chunk_array`` splits oversized arrays into <=1000-element
           chunks; user-facing methods that take a URN array fan out
           into N substrate calls and concatenate.
    P2.3   ``_assert_param_types_match`` invariant fires when params/
           param_types_ keys disagree — protects against silent type-
           coercion bugs across google-cloud-spanner upgrades.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.common.interfaces.provider import ProviderConfigurationError
from backend.common.providers.config import ProviderEnvBudget
from backend.graph.adapters import spanner_async_seam
from backend.graph.adapters.spanner_provider import (
    SpannerProvider,
    _DEFAULT_MERGE_BATCH,
    _validate_identifier,
)


# ──────────────────────────────────────────────────────────────────
# Helpers — same shape as Phase 1 test fixture builder
# ──────────────────────────────────────────────────────────────────


def _make_provider(*, graph_name: str = "TestGraph") -> SpannerProvider:
    p = SpannerProvider(
        project_id="p",
        instance_id="i",
        database_id="d",
        graph_name=graph_name,
        use_emulator=False,
    )
    p._client = object()
    p._instance = object()
    p._database = MagicMock(name="StubDatabase")
    p._connected = True
    p._schema_bootstrapped = True
    p._has_property_graph = True
    return p


def _shrink_budget(p: SpannerProvider, *, query: float) -> None:
    cur = p._budget
    p._budget = ProviderEnvBudget(
        query=query, write=cur.write, init=cur.init, purge_batch=cur.purge_batch,
    )


# ──────────────────────────────────────────────────────────────────
# P2.1 — graph_name validation
# ──────────────────────────────────────────────────────────────────


def test_validate_identifier_accepts_valid_names():
    assert _validate_identifier("UniViz", what="graph_name") == "UniViz"
    assert _validate_identifier("_internal_2", what="graph_name") == "_internal_2"
    assert _validate_identifier("a" * 128, what="graph_name") == "a" * 128


@pytest.mark.parametrize("bad_name", [
    "X NODE TABLES (Other AS Entity ...)",  # SQL injection attempt from audit B6
    "graph; DROP TABLE Users; --",
    "with-dashes",
    "1starts_with_digit",
    "has spaces",
    "a" * 129,  # too long
    "",  # empty
    "valid_name`backtick",
])
def test_graph_name_rejects_malicious_or_invalid(bad_name):
    with pytest.raises(ProviderConfigurationError) as exc:
        SpannerProvider(
            project_id="p",
            instance_id="i",
            database_id="d",
            graph_name=bad_name,
        )
    msg = str(exc.value)
    assert "graph_name" in msg
    assert "Invalid Spanner identifier" in msg


def test_graph_name_default_passes():
    """The default UniViz must always validate; otherwise existing
    deployments that omit graphName from extra_config break."""
    p = SpannerProvider(project_id="p", instance_id="i", database_id="d")
    assert p._graph_name == "UniViz"


# ──────────────────────────────────────────────────────────────────
# P2.2 — _chunk_array helper + chunked UNNEST sites
# ──────────────────────────────────────────────────────────────────


def test_chunk_array_basic():
    items = list(range(2500))
    chunks = list(SpannerProvider._chunk_array(items, chunk_size=1000))
    assert len(chunks) == 3
    assert len(chunks[0]) == 1000
    assert len(chunks[1]) == 1000
    assert len(chunks[2]) == 500
    assert sum(len(c) for c in chunks) == 2500


def test_chunk_array_uses_default_when_unspecified():
    chunks = list(SpannerProvider._chunk_array(list(range(_DEFAULT_MERGE_BATCH * 2 + 7))))
    assert len(chunks) == 3
    assert all(len(c) <= _DEFAULT_MERGE_BATCH for c in chunks)


def test_chunk_array_empty_yields_empty_chunk():
    chunks = list(SpannerProvider._chunk_array([]))
    assert chunks == [[]]


def test_chunk_array_invalid_chunk_size():
    with pytest.raises(ValueError):
        list(SpannerProvider._chunk_array([1, 2, 3], chunk_size=0))


async def test_get_nodes_batch_chunks_oversized_input():
    """A 3000-URN batch must produce exactly 3 substrate calls of 1000
    each, then concatenate. Audit M8."""
    p = _make_provider()
    call_sizes: List[int] = []

    class _Cursor:
        fields = (MagicMock(name="urn"), MagicMock(name="label"), MagicMock(name="properties"))
        def __init__(self, n_rows: int):
            self._n = n_rows
        def __iter__(self):
            for i in range(self._n):
                yield (f"urn:{i}", "Entity", '{"displayName":"x"}')

    class _Snapshot:
        def execute_sql(self, sql, *, params=None, param_types=None, timeout=None):
            urns = (params or {}).get("urns", [])
            call_sizes.append(len(urns))
            # Echo back rows for each requested urn.
            return _Cursor(len(urns))
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False

    # Patch fields to expose .name attribute the way real spanner cursors do.
    for f in _Cursor.fields:
        f.configure_mock(name="urn")
    _Cursor.fields[1].configure_mock(name="label")
    _Cursor.fields[2].configure_mock(name="properties")

    p._database.snapshot = lambda: _Snapshot()

    urns = [f"urn:test:{i}" for i in range(3000)]
    nodes = await p.get_nodes_batch(urns)

    assert call_sizes == [1000, 1000, 1000], (
        f"expected 3 chunks of 1000, got {call_sizes}"
    )
    assert len(nodes) == 3000


async def test_get_nodes_batch_single_call_when_under_chunk_size():
    p = _make_provider()
    call_sizes: List[int] = []

    class _Cursor:
        fields = ()
        def __iter__(self):
            return iter([])

    class _Snapshot:
        def execute_sql(self, sql, *, params=None, param_types=None, timeout=None):
            urns = (params or {}).get("urns", [])
            call_sizes.append(len(urns))
            return _Cursor()
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False

    p._database.snapshot = lambda: _Snapshot()
    await p.get_nodes_batch([f"urn:{i}" for i in range(50)])
    assert call_sizes == [50]


async def test_get_nodes_batch_empty_skips_substrate():
    p = _make_provider()
    p._database.snapshot = MagicMock(side_effect=AssertionError("substrate must not be called"))
    result = await p.get_nodes_batch([])
    assert result == []


# ──────────────────────────────────────────────────────────────────
# P2.3 — _assert_param_types_match invariant
# ──────────────────────────────────────────────────────────────────


def test_assert_param_types_match_passes_on_aligned():
    SpannerProvider._assert_param_types_match(
        {"a": 1, "b": 2}, {"a": object(), "b": object()},
    )
    SpannerProvider._assert_param_types_match(None, None)
    SpannerProvider._assert_param_types_match({}, {})


def test_assert_param_types_match_rejects_extra_in_types():
    with pytest.raises(ProviderConfigurationError) as exc:
        SpannerProvider._assert_param_types_match(
            {"a": 1}, {"a": object(), "b": object()},
        )
    assert "types-only: ['b']" in str(exc.value)


def test_assert_param_types_match_rejects_extra_in_params():
    with pytest.raises(ProviderConfigurationError) as exc:
        SpannerProvider._assert_param_types_match(
            {"a": 1, "b": 2}, {"a": object()},
        )
    assert "params-only: ['b']" in str(exc.value)


async def test_execute_query_invariant_fires_at_substrate_boundary():
    """Calling _execute_query with mismatched param/types raises before
    any network I/O. This is the new defense the substrate provides."""
    p = _make_provider()
    p._database.snapshot = MagicMock(side_effect=AssertionError("must not reach substrate"))
    with pytest.raises(ProviderConfigurationError):
        await p._execute_query(
            "SELECT 1",
            op_name="test_invariant",
            params={"a": 1},
            param_types_={"b": object()},
        )


# ──────────────────────────────────────────────────────────────────
# Bonus — confirm get_aggregated_edges_between is now symmetric
# ──────────────────────────────────────────────────────────────────


async def test_get_aggregated_edges_between_no_targets_passes_invariant():
    """Audit M9 specifically: param_types_ used to declare 'dsts' even
    when params didn't. Post-fix, the call must succeed (no spurious
    invariant trip) when target_urns is None."""
    p = _make_provider()

    class _Cursor:
        fields = ()
        def __iter__(self):
            return iter([])

    class _Snapshot:
        def execute_sql(self, sql, *, params=None, param_types=None, timeout=None):
            # The substrate's invariant would have raised by now if the
            # call dict was asymmetric.
            assert set(params.keys()) == set(param_types.keys()), (
                f"params/types asymmetric: params={set(params)}, types={set(param_types)}"
            )
            return _Cursor()
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False

    p._database.snapshot = lambda: _Snapshot()
    p._has_property_graph = True
    p._connected = True

    result = await p.get_aggregated_edges_between(
        source_urns=["urn:1", "urn:2"],
        target_urns=None,
        granularity=None,
        containment_edges=[],
        lineage_edges=[],
    )
    assert result.aggregated_edges == []
