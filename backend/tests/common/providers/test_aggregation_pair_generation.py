"""Unit tests for AggregatedEdgeMaterializer pair generation and idempotency.

Uses an in-memory IdempotencyBackend + MaterializationBackend so the
algorithm is exercised without any database dependency.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import pytest

from backend.common.providers.aggregation import (
    AggregatedEdgeMaterializer,
    AncestorResolver,
    IdempotencyBackend,
    MaterializationBackend,
    MaterializerConfig,
    PairKey,
    _cross_pairs,
)


# ---------------------------------------------------------------------------
# Pair generation
# ---------------------------------------------------------------------------

def test_cross_pairs_skips_self_loops_and_dedupes():
    s = ["a", "b", "common"]
    t = ["x", "y", "common"]
    pairs = list(_cross_pairs(s, t))
    # No (common, common) self-loop.
    assert ("common", "common") not in pairs
    # Cardinality: |s|*|t| - 1 (the dropped self-loop).
    assert len(pairs) == len(s) * len(t) - 1
    assert ("a", "common") in pairs
    assert ("common", "x") in pairs


def test_cross_pairs_empty():
    assert list(_cross_pairs([], ["x"])) == []
    assert list(_cross_pairs(["a"], [])) == []


# ---------------------------------------------------------------------------
# In-memory backends
# ---------------------------------------------------------------------------

class _MemIdempotency(IdempotencyBackend):
    def __init__(self) -> None:
        self.members: Dict[PairKey, set] = defaultdict(set)
        self.types: Dict[PairKey, set] = defaultdict(set)

    async def add(self, pair, edge_id, edge_type):
        before = len(self.members[pair])
        self.members[pair].add(edge_id)
        if edge_type:
            self.types[pair].add(edge_type)
        return len(self.members[pair]) > before

    async def remove(self, pair, edge_id):
        if edge_id in self.members[pair]:
            self.members[pair].remove(edge_id)
            return True
        return False

    async def count(self, pair):
        return len(self.members[pair])

    async def edge_types(self, pair):
        return sorted(self.types[pair])

    async def purge_namespace(self):
        self.members.clear()
        self.types.clear()


class _MemMaterialization(MaterializationBackend):
    def __init__(self) -> None:
        self.upserts: List[Tuple[PairKey, int, List[str]]] = []
        self.deletes: List[PairKey] = []
        self.updates: List[Tuple[PairKey, int, List[str]]] = []

    async def upsert_aggregated_edges(self, pairs):
        self.upserts.extend(pairs)

    async def delete_aggregated_edge(self, pair):
        self.deletes.append(pair)

    async def update_aggregated_edge(self, pair, weight, source_edge_types):
        self.updates.append((pair, weight, list(source_edge_types)))

    async def count_all(self):
        return len({p for p, _, _ in self.upserts}) - len(self.deletes)

    async def purge_all(self, *, batch_size, progress_callback=None, should_cancel=None):
        return 0


class _StaticAncestors(AncestorResolver):
    def __init__(self, mapping: Dict[str, List[str]]) -> None:
        self._m = mapping

    async def chain(self, urn):
        return list(self._m.get(urn, [urn]))

    async def chains(self, urns):
        return {u: list(self._m.get(u, [u])) for u in urns}


def _materializer(
    ancestors: Dict[str, List[str]],
) -> Tuple[AggregatedEdgeMaterializer, _MemIdempotency, _MemMaterialization]:
    idem = _MemIdempotency()
    mat = _MemMaterialization()
    m = AggregatedEdgeMaterializer(
        idempotency=idem,
        materialization=mat,
        ancestors=_StaticAncestors(ancestors),
        config=MaterializerConfig(batch_size=10),
    )
    return m, idem, mat


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_materialize_writes_one_upsert_per_unique_pair():
    m, idem, mat = _materializer({
        "leaf1": ["leaf1", "schema1", "domain"],
        "leaf2": ["leaf2", "schema2", "domain"],
    })
    await m.materialize_lineage_edge("leaf1", "leaf2", "edge1", "DERIVES_FROM")

    pairs_written = {p for p, _, _ in mat.upserts}
    # 3 source ancestors x 3 target ancestors - 1 self-loop ("domain","domain").
    assert len(pairs_written) == 8
    assert ("leaf1", "leaf2") in pairs_written
    assert ("schema1", "domain") in pairs_written
    # Idempotency tracked.
    assert idem.members[("leaf1", "leaf2")] == {"edge1"}


@pytest.mark.asyncio
async def test_re_materialize_same_edge_is_idempotent():
    m, idem, mat = _materializer({
        "leaf1": ["leaf1", "schema1"],
        "leaf2": ["leaf2", "schema2"],
    })
    await m.materialize_lineage_edge("leaf1", "leaf2", "edge1", "T")
    upserts_after_first = list(mat.upserts)

    await m.materialize_lineage_edge("leaf1", "leaf2", "edge1", "T")

    # Idempotency backend rejects the duplicate; no new upserts emitted.
    assert mat.upserts == upserts_after_first
    assert idem.members[("leaf1", "leaf2")] == {"edge1"}


@pytest.mark.asyncio
async def test_remove_drops_pair_when_count_reaches_zero():
    m, idem, mat = _materializer({
        "leaf1": ["leaf1", "schema1"],
        "leaf2": ["leaf2", "schema2"],
    })
    await m.materialize_lineage_edge("leaf1", "leaf2", "edge1", "T")
    await m.remove_lineage_edge("leaf1", "leaf2", "edge1")

    # Every pair from the original cross-product should be deleted.
    assert ("leaf1", "leaf2") in mat.deletes
    assert ("schema1", "schema2") in mat.deletes
    assert all(len(s) == 0 for s in idem.members.values())


@pytest.mark.asyncio
async def test_remove_keeps_pair_when_other_edges_contribute():
    m, idem, mat = _materializer({
        "leaf1": ["leaf1", "schema1"],
        "leaf2": ["leaf2", "schema2"],
    })
    # Two distinct leaf edges share the (schema1, schema2) AGGREGATED pair.
    await m.materialize_lineage_edge("leaf1", "leaf2", "edgeA", "T")
    await m.materialize_lineage_edge("leaf1", "leaf2", "edgeB", "T")

    await m.remove_lineage_edge("leaf1", "leaf2", "edgeA")

    # The (schema1, schema2) pair still has edgeB contributing -> NOT deleted.
    deleted = set(mat.deletes)
    updated = {p for p, _, _ in mat.updates}
    assert ("schema1", "schema2") not in deleted
    assert ("schema1", "schema2") in updated


@pytest.mark.asyncio
async def test_purge_all_drops_idempotency_after_graph_purge():
    m, idem, mat = _materializer({
        "leaf1": ["leaf1", "schema1"],
        "leaf2": ["leaf2", "schema2"],
    })
    await m.materialize_lineage_edge("leaf1", "leaf2", "edge1", "T")
    assert any(idem.members.values())

    await m.purge_all()

    assert not any(idem.members.values()), "idempotency state must be cleared post-purge"
