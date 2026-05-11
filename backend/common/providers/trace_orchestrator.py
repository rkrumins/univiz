"""Trace v2 set-based BFS orchestrator.

The algorithm is identical across providers; only the per-hop query
template differs. This module owns the algorithm:

* anchor resolution (climb containment if focus is below the requested level)
* inherited-lineage fallback (anchor at nearest ancestor with AGGREGATED edges
  when the requested anchor has none)
* per-hop parallel up/down expansion via ``asyncio.gather``
* deadline tracking with ``time.monotonic()``
* truncation with reason ("timeout" | "max_nodes")
* containment-chain hydration of the result so the canvas can position nodes

Providers expose database-specific behaviour via the ``TraceCallbacks`` Protocol.
The orchestrator never imports provider-specific code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Protocol, Set

from backend.common.models.graph import GraphEdge, GraphNode, TraceFocus, TraceResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback Protocol
# ---------------------------------------------------------------------------

@dataclass
class FrontierRecord:
    """One per-hop expansion record returned by the provider.

    ``new_urn`` is the URN on the "other side" of the edge (the one BFS
    has just discovered). The orchestrator decides whether it's already
    visited and adds it to the appropriate frontier.
    """
    edge_id: str
    source_urn: str
    target_urn: str
    new_urn: str
    edge_type: str = "AGGREGATED"
    weight: int = 1
    source_edge_types: List[str] = field(default_factory=list)
    new_node: Optional[GraphNode] = None  # provider may hydrate inline


@dataclass
class ExpandRecord:
    """Record returned during expand_aggregated drill-down."""
    edge_id: str
    source_urn: str
    target_urn: str
    edge_type: str = "AGGREGATED"
    weight: int = 1
    source_edge_types: List[str] = field(default_factory=list)


class TraceCallbacks(Protocol):
    """Provider-supplied behaviour for the orchestrator.

    All callbacks are async. Implementations should bound their I/O via
    the provider's own DeadlineGuard; the orchestrator does not enforce
    per-callback deadlines (it owns the parent budget only).
    """

    async def get_node(self, urn: str) -> Optional[GraphNode]: ...

    async def get_nodes_batch(self, urns: List[str]) -> List[GraphNode]: ...

    async def get_node_level(self, entity_type: str) -> Optional[int]: ...

    async def resolve_anchor_at_level(
        self,
        urn: str,
        level: int,
        containment_edge_types: List[str],
    ) -> str:
        """Climb containment from ``urn`` to the nearest ancestor at ``level``.

        Returns the input ``urn`` unchanged if it is already at-or-above
        the level, or no ancestor exists at that level.
        """
        ...

    async def has_aggregated_at_level(
        self, urn: str, level: int,
    ) -> bool: ...

    async def find_ancestor_with_lineage(
        self,
        urn: str,
        level: int,
        containment_edge_types: List[str],
    ) -> Optional[str]:
        """Walk UP containment to first ancestor that has AGGREGATED edges
        at ``level``. Used for the inherited-lineage fallback.
        """
        ...

    async def expand_frontier(
        self,
        urns: List[str],
        *,
        direction: str,        # "incoming" (upstream) | "outgoing" (downstream)
        level: int,
        lineage_edge_types: Optional[List[str]],
        budget: int,
    ) -> List[FrontierRecord]: ...

    async def collect_ancestor_urns(
        self,
        urns: List[str],
        containment_edge_types: List[str],
    ) -> List[str]:
        """Return all containment ancestors of ``urns`` (used to hydrate
        the layered-hierarchy context after BFS completes).
        """
        ...

    async def fetch_containment_edges(
        self,
        node_urns: List[str],
        containment_edge_types: List[str],
    ) -> List[GraphEdge]: ...

    async def descendants_at_level(
        self,
        anchor_urn: str,
        level: int,
        containment_edge_types: List[str],
    ) -> Set[str]:
        """Collect descendant URNs at ``level`` under ``anchor_urn``.
        Used by ``expand_aggregated`` to build the source/target sets.
        """
        ...

    async def edges_between(
        self,
        source_urns: List[str],
        target_urns: List[str],
        edge_types: Optional[List[str]],
        *,
        use_raw_edges: bool = False,
    ) -> List[ExpandRecord]: ...


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class TraceOrchestrator:
    """Implements ``trace_at_level`` and ``expand_aggregated`` once."""

    def __init__(self, callbacks: TraceCallbacks) -> None:
        self._cb = callbacks

    # ----- trace_at_level ---------------------------------------------------

    async def trace_at_level(
        self,
        *,
        urn: str,
        level: int,
        upstream_depth: int,
        downstream_depth: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        include_containment_edges: bool = False,  # ignored; hierarchy always included
        include_inherited_lineage: bool = True,
    ) -> TraceResult:
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        # Edge-type filter values are forwarded to provider callbacks
        # verbatim. Providers that need case-folding (e.g. Cypher-canonical
        # backends) normalise inside the callback; Spanner schemaless
        # treats labels as case-sensitive data.
        ctypes = list(containment_edge_types or [])
        ltypes = list(lineage_edge_types) if lineage_edge_types else None

        focus_node = await self._cb.get_node(urn)
        focus_entity_type = focus_node.entity_type if focus_node else "unknown"

        # 1. Resolve anchor at the requested level.
        anchor_urn = await self._cb.resolve_anchor_at_level(urn, level, ctypes)

        # 2. Inherited-lineage fallback: if anchor has no AGGREGATED edges at
        #    this level, climb to first ancestor that does.
        is_inherited = False
        inherited_from: Optional[str] = None
        if include_inherited_lineage and not await self._cb.has_aggregated_at_level(anchor_urn, level):
            parent = await self._cb.find_ancestor_with_lineage(anchor_urn, level, ctypes)
            if parent and parent != anchor_urn:
                inherited_from = anchor_urn
                anchor_urn = parent
                is_inherited = True

        # 3. Set-based BFS state.
        nodes_by_urn: Dict[str, GraphNode] = {}
        anchor_node = await self._cb.get_node(anchor_urn)
        if anchor_node is not None:
            nodes_by_urn[anchor_urn] = anchor_node
        edges_by_id: Dict[str, GraphEdge] = {}
        upstream_urns: Set[str] = set()
        downstream_urns: Set[str] = set()
        visited: Set[str] = {anchor_urn}
        up_frontier: Set[str] = {anchor_urn} if upstream_depth > 0 else set()
        down_frontier: Set[str] = {anchor_urn} if downstream_depth > 0 else set()
        truncation_reason: Optional[str] = None

        # 4. Per-hop parallel expansion.
        start_monotonic = deadline - (timeout_ms / 1000.0)
        for hop in range(max(upstream_depth, downstream_depth)):
            if time.monotonic() > deadline:
                truncation_reason = "timeout"
                logger.warning(
                    "trace_at_level truncated",
                    extra={
                        "reason": "timeout",
                        "focus_urn": urn,
                        "level": level,
                        "hop": hop,
                        "nodes_collected": len(nodes_by_urn),
                        "edges_collected": len(edges_by_id),
                        "elapsed_ms": int((time.monotonic() - start_monotonic) * 1000),
                        "max_nodes": max_nodes,
                        "timeout_ms": timeout_ms,
                        "upstream_depth": upstream_depth,
                        "downstream_depth": downstream_depth,
                    },
                )
                break
            if len(nodes_by_urn) >= max_nodes:
                truncation_reason = "max_nodes"
                logger.warning(
                    "trace_at_level truncated",
                    extra={
                        "reason": "max_nodes",
                        "focus_urn": urn,
                        "level": level,
                        "hop": hop,
                        "nodes_collected": len(nodes_by_urn),
                        "edges_collected": len(edges_by_id),
                        "elapsed_ms": int((time.monotonic() - start_monotonic) * 1000),
                        "max_nodes": max_nodes,
                        "timeout_ms": timeout_ms,
                        "upstream_depth": upstream_depth,
                        "downstream_depth": downstream_depth,
                    },
                )
                break
            budget = max_nodes - len(nodes_by_urn)

            tasks: List[tuple[str, Awaitable[List[FrontierRecord]]]] = []
            if hop < upstream_depth and up_frontier:
                tasks.append(("up", self._cb.expand_frontier(
                    list(up_frontier), direction="incoming",
                    level=level, lineage_edge_types=ltypes, budget=budget,
                )))
            if hop < downstream_depth and down_frontier:
                tasks.append(("down", self._cb.expand_frontier(
                    list(down_frontier), direction="outgoing",
                    level=level, lineage_edge_types=ltypes, budget=budget,
                )))
            if not tasks:
                break

            results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)

            new_up: Set[str] = set()
            new_down: Set[str] = set()
            for (direction, _), recs in zip(tasks, results):
                if isinstance(recs, Exception):
                    logger.warning("trace_at_level expand (%s) failed: %s", direction, recs)
                    continue
                for rec in recs:
                    if rec.edge_id not in edges_by_id:
                        edges_by_id[rec.edge_id] = GraphEdge(
                            id=rec.edge_id,
                            sourceUrn=rec.source_urn,
                            targetUrn=rec.target_urn,
                            edgeType=rec.edge_type,
                            properties={
                                "sourceEdgeTypes": list(rec.source_edge_types or []),
                                "weight": rec.weight,
                            },
                        )
                    if rec.new_node is not None and rec.new_urn not in nodes_by_urn:
                        nodes_by_urn[rec.new_urn] = rec.new_node
                    if rec.new_urn not in visited:
                        visited.add(rec.new_urn)
                        if direction == "up":
                            new_up.add(rec.new_urn)
                            upstream_urns.add(rec.new_urn)
                        else:
                            new_down.add(rec.new_urn)
                            downstream_urns.add(rec.new_urn)

            up_frontier = new_up
            down_frontier = new_down
            if not up_frontier and not down_frontier:
                break

        # 5. Hydrate any nodes we discovered URN-only (provider returned no node inline).
        missing_node_urns = [u for u in visited if u not in nodes_by_urn]
        if missing_node_urns:
            try:
                fetched = await self._cb.get_nodes_batch(missing_node_urns)
                for n in fetched:
                    nodes_by_urn[n.urn] = n
            except Exception as exc:
                logger.warning("trace_at_level: node hydration failed: %s", exc)

        # 6. Always hydrate containment ancestors so the canvas has hierarchy.
        containment_edges_list: List[GraphEdge] = []
        if ctypes and nodes_by_urn:
            try:
                ancestor_urns = await self._cb.collect_ancestor_urns(list(nodes_by_urn.keys()), ctypes)
                new_ancestors = [u for u in ancestor_urns if u not in nodes_by_urn]
                if new_ancestors:
                    ancestor_nodes = await self._cb.get_nodes_batch(new_ancestors)
                    for n in ancestor_nodes:
                        nodes_by_urn[n.urn] = n
                if nodes_by_urn:
                    containment_edges_list = await self._cb.fetch_containment_edges(
                        list(nodes_by_urn.keys()), ctypes,
                    )
            except Exception as exc:
                logger.warning("trace_at_level: containment hydration failed: %s", exc)

        focus_level = await self._cb.get_node_level(focus_entity_type) or level

        return TraceResult(
            nodes=list(nodes_by_urn.values()),
            edges=list(edges_by_id.values()),
            containmentEdges=containment_edges_list,
            upstreamUrns=upstream_urns,
            downstreamUrns=downstream_urns,
            focus=TraceFocus(urn=urn, level=focus_level, entityType=focus_entity_type),
            effectiveLevel=level,
            isInherited=is_inherited,
            inheritedFromUrn=inherited_from,
            truncated=truncation_reason is not None,
            truncationReason=truncation_reason,
        )

    # ----- expand_aggregated ------------------------------------------------

    async def expand_aggregated(
        self,
        *,
        source_urn: str,
        target_urn: str,
        next_level: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        use_raw_edges: bool = False,
        include_containment_edges: bool = False,  # ignored; hierarchy always included
    ) -> TraceResult:
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        # See ``trace_at_level`` for the case-folding rationale: pass
        # edge-type values through to provider callbacks verbatim.
        ctypes = list(containment_edge_types or [])
        ltypes = list(lineage_edge_types) if lineage_edge_types else None

        # 1. Collect descendants of source/target at next_level (parallel).
        src_task = self._cb.descendants_at_level(source_urn, next_level, ctypes)
        dst_task = self._cb.descendants_at_level(target_urn, next_level, ctypes)
        src_set, dst_set = await asyncio.gather(src_task, dst_task)

        truncation_reason: Optional[str] = None
        if len(src_set) + len(dst_set) > max_nodes:
            truncation_reason = "max_nodes"
            # Trim deterministically (sort for stable test output).
            src_set = set(list(sorted(src_set))[:max_nodes // 2])
            dst_set = set(list(sorted(dst_set))[:max_nodes // 2])

        # 2. Edges between the two URN sets — one set-based query, no Cartesian.
        if time.monotonic() > deadline:
            return self._empty_expand_result(source_urn, target_urn, next_level, "timeout")

        try:
            recs = await self._cb.edges_between(
                list(src_set), list(dst_set), ltypes, use_raw_edges=use_raw_edges,
            )
        except Exception as exc:
            logger.warning("expand_aggregated: edges_between failed: %s", exc)
            recs = []

        edges_by_id: Dict[str, GraphEdge] = {}
        for rec in recs:
            if rec.edge_id in edges_by_id:
                continue
            edges_by_id[rec.edge_id] = GraphEdge(
                id=rec.edge_id,
                sourceUrn=rec.source_urn,
                targetUrn=rec.target_urn,
                edgeType=rec.edge_type,
                properties={
                    "sourceEdgeTypes": list(rec.source_edge_types or []),
                    "weight": rec.weight,
                },
            )

        # 3. Hydrate nodes referenced by edges + the two anchor sets.
        all_urns = set(src_set) | set(dst_set)
        for e in edges_by_id.values():
            all_urns.add(e.source_urn)
            all_urns.add(e.target_urn)

        nodes_by_urn: Dict[str, GraphNode] = {}
        if all_urns:
            try:
                fetched = await self._cb.get_nodes_batch(list(all_urns))
                for n in fetched:
                    nodes_by_urn[n.urn] = n
            except Exception as exc:
                logger.warning("expand_aggregated: node hydration failed: %s", exc)

        # 4. Containment context.
        containment_edges_list: List[GraphEdge] = []
        if ctypes and nodes_by_urn:
            try:
                ancestor_urns = await self._cb.collect_ancestor_urns(list(nodes_by_urn.keys()), ctypes)
                new_ancestors = [u for u in ancestor_urns if u not in nodes_by_urn]
                if new_ancestors:
                    ancestor_nodes = await self._cb.get_nodes_batch(new_ancestors)
                    for n in ancestor_nodes:
                        nodes_by_urn[n.urn] = n
                if nodes_by_urn:
                    containment_edges_list = await self._cb.fetch_containment_edges(
                        list(nodes_by_urn.keys()), ctypes,
                    )
            except Exception as exc:
                logger.warning("expand_aggregated: containment hydration failed: %s", exc)

        # 5. Focus = source anchor for response shape compatibility.
        focus_node = nodes_by_urn.get(source_urn) or await self._cb.get_node(source_urn)
        focus_entity_type = focus_node.entity_type if focus_node else "unknown"
        focus_level = await self._cb.get_node_level(focus_entity_type) or next_level

        upstream_urns: Set[str] = set(src_set)
        downstream_urns: Set[str] = set(dst_set)

        return TraceResult(
            nodes=list(nodes_by_urn.values()),
            edges=list(edges_by_id.values()),
            containmentEdges=containment_edges_list,
            upstreamUrns=upstream_urns,
            downstreamUrns=downstream_urns,
            focus=TraceFocus(urn=source_urn, level=focus_level, entityType=focus_entity_type),
            effectiveLevel=next_level,
            isInherited=False,
            inheritedFromUrn=None,
            truncated=truncation_reason is not None,
            truncationReason=truncation_reason,
        )

    # ----- helpers ----------------------------------------------------------

    def _empty_expand_result(
        self, source_urn: str, target_urn: str, level: int, reason: Optional[str],
    ) -> TraceResult:
        return TraceResult(
            nodes=[], edges=[], containmentEdges=[],
            upstreamUrns=set(), downstreamUrns=set(),
            focus=TraceFocus(urn=source_urn, level=level, entityType="unknown"),
            effectiveLevel=level,
            isInherited=False,
            inheritedFromUrn=None,
            truncated=reason is not None,
            truncationReason=reason,
        )
