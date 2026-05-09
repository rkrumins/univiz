"""Mock-driven tests for TraceOrchestrator.

The orchestrator is the shared base behind ``trace_at_level`` and
``expand_aggregated``; Phase B/C will route FalkorDB+Neo4j through it
once their snapshot baselines are captured. To keep that reshape safe
we exercise the algorithm here against an in-memory mock that
simulates a small graph — every BFS branch, deadline truncation,
inherited-lineage fallback, and node hydration path is covered without
needing a real database.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import pytest

from backend.common.models.graph import GraphEdge, GraphNode, TraceResult
from backend.common.providers.trace_orchestrator import (
    ExpandRecord,
    FrontierRecord,
    TraceCallbacks,
    TraceOrchestrator,
)


# ---------------------------------------------------------------------------
# Tiny in-memory graph and a TraceCallbacks shim
# ---------------------------------------------------------------------------

class _MockGraph:
    """A 5-node test fixture: domain → schema → {leaf1, leaf2}.

    Lineage: leaf1 -> leaf2 (DERIVES_FROM), schema -> schema_other (FLOWS_TO).
    AGGREGATED edges live at the schema level: schema -> schema_other.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, GraphNode] = {
            "urn:domain": GraphNode(urn="urn:domain", entityType="domain", displayName="Root"),
            "urn:schema": GraphNode(urn="urn:schema", entityType="schema", displayName="Schema"),
            "urn:schema:other": GraphNode(urn="urn:schema:other", entityType="schema", displayName="Schema Other"),
            "urn:leaf1": GraphNode(urn="urn:leaf1", entityType="dataset", displayName="Leaf1"),
            "urn:leaf2": GraphNode(urn="urn:leaf2", entityType="dataset", displayName="Leaf2"),
        }
        # Containment ancestor chains (incoming containment).
        self.ancestors: Dict[str, List[str]] = {
            "urn:leaf1": ["urn:schema", "urn:domain"],
            "urn:leaf2": ["urn:schema:other", "urn:domain"],
            "urn:schema": ["urn:domain"],
            "urn:schema:other": ["urn:domain"],
            "urn:domain": [],
        }
        # AGGREGATED edges keyed by source: each list element is (target, edge_type, weight, src_types)
        self.outgoing_agg: Dict[str, List[Tuple[str, str, int, List[str]]]] = {
            "urn:schema": [("urn:schema:other", "AGGREGATED", 3, ["DERIVES_FROM"])],
        }
        # entity-type-id → level
        self.levels: Dict[str, int] = {"domain": 0, "schema": 1, "dataset": 2}
        # Track call counts for assertions.
        self.calls: Dict[str, int] = defaultdict(int)


class _MockCallbacks(TraceCallbacks):
    def __init__(
        self,
        graph: _MockGraph,
        *,
        anchor_at_level: Optional[Dict[Tuple[str, int], str]] = None,
        has_aggregated: Optional[Dict[Tuple[str, int], bool]] = None,
        ancestor_with_lineage: Optional[Dict[Tuple[str, int], Optional[str]]] = None,
    ) -> None:
        self.g = graph
        self._anchor_at_level = anchor_at_level or {}
        self._has_aggregated = has_aggregated or {}
        self._ancestor_with_lineage = ancestor_with_lineage or {}

    async def get_node(self, urn):
        self.g.calls["get_node"] += 1
        return self.g.nodes.get(urn)

    async def get_nodes_batch(self, urns):
        self.g.calls["get_nodes_batch"] += 1
        return [self.g.nodes[u] for u in urns if u in self.g.nodes]

    async def get_node_level(self, entity_type):
        return self.g.levels.get(entity_type)

    async def resolve_anchor_at_level(self, urn, level, containment_edge_types):
        return self._anchor_at_level.get((urn, level), urn)

    async def has_aggregated_at_level(self, urn, level):
        return self._has_aggregated.get((urn, level), False)

    async def find_ancestor_with_lineage(self, urn, level, containment_edge_types):
        return self._ancestor_with_lineage.get((urn, level))

    async def expand_frontier(self, urns, *, direction, level, lineage_edge_types, budget):
        self.g.calls[f"expand_frontier_{direction}"] += 1
        out: List[FrontierRecord] = []
        for u in urns:
            if direction == "outgoing":
                for target, edge_type, weight, src_types in self.g.outgoing_agg.get(u, []):
                    out.append(FrontierRecord(
                        edge_id=f"e:{u}->{target}",
                        source_urn=u, target_urn=target, new_urn=target,
                        edge_type=edge_type, weight=weight,
                        source_edge_types=src_types,
                        new_node=self.g.nodes.get(target),
                    ))
            else:  # incoming
                for src, edges in self.g.outgoing_agg.items():
                    for target, edge_type, weight, src_types in edges:
                        if target == u:
                            out.append(FrontierRecord(
                                edge_id=f"e:{src}->{target}",
                                source_urn=src, target_urn=target, new_urn=src,
                                edge_type=edge_type, weight=weight,
                                source_edge_types=src_types,
                                new_node=self.g.nodes.get(src),
                            ))
        return out[:budget] if budget else out

    async def collect_ancestor_urns(self, urns, containment_edge_types):
        seen: Set[str] = set()
        for u in urns:
            seen.update(self.g.ancestors.get(u, []))
        return sorted(seen)

    async def fetch_containment_edges(self, node_urns, containment_edge_types):
        # Synthesise a simple parent->child edge set from the ancestor map.
        out: List[GraphEdge] = []
        node_set = set(node_urns)
        for child, ancestors in self.g.ancestors.items():
            if child not in node_set or not ancestors:
                continue
            parent = ancestors[0]
            if parent in node_set:
                out.append(GraphEdge(
                    id=f"c:{parent}->{child}",
                    sourceUrn=parent, targetUrn=child, edgeType="CONTAINS",
                ))
        return out

    async def descendants_at_level(self, anchor_urn, level, containment_edge_types):
        out: Set[str] = set()
        for urn, ancestors in self.g.ancestors.items():
            if anchor_urn in ancestors:
                node = self.g.nodes.get(urn)
                if node and self.g.levels.get(node.entity_type) == level:
                    out.add(urn)
        return out

    async def edges_between(self, source_urns, target_urns, edge_types, *, use_raw_edges=False):
        srcs, dsts = set(source_urns), set(target_urns)
        out: List[ExpandRecord] = []
        for s, edges in self.g.outgoing_agg.items():
            if s not in srcs:
                continue
            for target, edge_type, weight, src_types in edges:
                if target not in dsts:
                    continue
                out.append(ExpandRecord(
                    edge_id=f"e:{s}->{target}",
                    source_urn=s, target_urn=target,
                    edge_type=edge_type, weight=weight,
                    source_edge_types=src_types,
                ))
        return out


# ---------------------------------------------------------------------------
# Fast paths: anchor resolution and basic BFS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_at_level_returns_anchor_when_no_lineage_at_level():
    g = _MockGraph()
    cb = _MockCallbacks(g)
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=2,           # dataset level — no AGGREGATED here
        upstream_depth=2, downstream_depth=2,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
    )

    # Anchor itself in nodes; BFS expanded zero edges (none at dataset level).
    assert any(n.urn == "urn:leaf1" for n in res.nodes)
    assert res.edges == []
    assert res.truncated is False


@pytest.mark.asyncio
async def test_trace_at_level_hydrates_aggregated_at_correct_level():
    g = _MockGraph()
    cb = _MockCallbacks(g, anchor_at_level={("urn:leaf1", 1): "urn:schema"})
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,           # schema level — AGGREGATED edge here
        upstream_depth=2, downstream_depth=2,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
    )

    edge_ids = {e.id for e in res.edges}
    assert "e:urn:schema->urn:schema:other" in edge_ids
    nodes_by_urn = {n.urn: n for n in res.nodes}
    assert "urn:schema" in nodes_by_urn
    assert "urn:schema:other" in nodes_by_urn
    # Containment hierarchy hydrated for the canvas.
    assert "urn:domain" in nodes_by_urn


# ---------------------------------------------------------------------------
# Inherited-lineage fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inherited_lineage_climbs_to_ancestor_with_aggregated():
    g = _MockGraph()
    cb = _MockCallbacks(
        g,
        # leaf1 itself has no aggregated lineage at level=1; climb to schema.
        has_aggregated={("urn:leaf1", 1): False, ("urn:schema", 1): True},
        ancestor_with_lineage={("urn:leaf1", 1): "urn:schema"},
    )
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,
        upstream_depth=1, downstream_depth=1,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
        include_inherited_lineage=True,
    )

    assert res.is_inherited is True
    assert res.inherited_from_urn == "urn:leaf1"
    # The trace anchored on urn:schema and the AGGREGATED edge surfaces.
    assert any(e.id == "e:urn:schema->urn:schema:other" for e in res.edges)


@pytest.mark.asyncio
async def test_inherited_lineage_disabled_returns_empty_when_anchor_has_none():
    g = _MockGraph()
    cb = _MockCallbacks(
        g,
        has_aggregated={("urn:leaf1", 1): False},
        ancestor_with_lineage={("urn:leaf1", 1): "urn:schema"},  # would climb if asked
    )
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,
        upstream_depth=1, downstream_depth=1,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
        include_inherited_lineage=False,
    )

    # Did NOT climb — leaf1 stays as anchor; no AGGREGATED edge for leaf1.
    assert res.is_inherited is False
    assert res.inherited_from_urn is None


# ---------------------------------------------------------------------------
# Truncation: max_nodes and timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_truncates_on_max_nodes():
    g = _MockGraph()
    cb = _MockCallbacks(g, anchor_at_level={("urn:leaf1", 1): "urn:schema"})
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,
        upstream_depth=5, downstream_depth=5,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=1,                       # tiny budget — anchor only
        timeout_ms=5000,
    )

    # Anchor lands; BFS exits with max_nodes truncation reason.
    assert res.truncated is True
    assert res.truncation_reason == "max_nodes"


@pytest.mark.asyncio
async def test_trace_truncates_on_timeout():
    """Set timeout_ms=0 so the deadline is already past on entry."""
    g = _MockGraph()
    cb = _MockCallbacks(g, anchor_at_level={("urn:leaf1", 1): "urn:schema"})
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,
        upstream_depth=2, downstream_depth=2,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100,
        timeout_ms=0,
    )
    assert res.truncated is True
    assert res.truncation_reason == "timeout"


# ---------------------------------------------------------------------------
# expand_aggregated drill-down
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expand_aggregated_yields_edges_between_descendant_sets():
    # Wire up two schemas with a leaf each so descendants_at_level(level=2)
    # returns one URN per side; edges_between() yields the leaf-to-leaf edge.
    g = _MockGraph()
    g.outgoing_agg["urn:leaf1"] = [("urn:leaf2", "DERIVES_FROM", 1, ["DERIVES_FROM"])]
    cb = _MockCallbacks(g)
    orch = TraceOrchestrator(cb)

    res = await orch.expand_aggregated(
        source_urn="urn:schema",
        target_urn="urn:schema:other",
        next_level=2,                      # dataset level — leaf-to-leaf edges
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
    )

    assert any(e.id == "e:urn:leaf1->urn:leaf2" for e in res.edges)
    nodes = {n.urn for n in res.nodes}
    assert "urn:leaf1" in nodes
    assert "urn:leaf2" in nodes


# ---------------------------------------------------------------------------
# Containment hydration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_always_hydrates_containment_hierarchy():
    """Even when ``include_containment_edges=False``, the canvas needs the
    layered hierarchy to position trace nodes — orchestrator hydrates ancestors
    unconditionally."""
    g = _MockGraph()
    cb = _MockCallbacks(g, anchor_at_level={("urn:leaf1", 1): "urn:schema"})
    orch = TraceOrchestrator(cb)

    res = await orch.trace_at_level(
        urn="urn:leaf1", level=1,
        upstream_depth=1, downstream_depth=1,
        lineage_edge_types=["DERIVES_FROM"],
        containment_edge_types=["CONTAINS"],
        max_nodes=100, timeout_ms=5000,
        include_containment_edges=False,   # ignored — see orchestrator docstring
    )

    nodes = {n.urn for n in res.nodes}
    assert "urn:domain" in nodes, "domain ancestor should always be hydrated"
    assert any(e.source_urn == "urn:domain" for e in res.containment_edges)
