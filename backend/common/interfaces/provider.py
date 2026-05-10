"""
Abstract GraphDataProvider interface — shared kernel.
Both the visualization service and graph service import from here.
"""
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, List, Optional, Dict, Any

from ..models.graph import (
    GraphNode, GraphEdge, NodeQuery, EdgeQuery,
    LineageResult, GraphSchemaStats, OntologyMetadata,
    ChildrenWithEdgesResult, TopLevelNodesResult,
    TraceResult,
)


class ProviderConfigurationError(RuntimeError):
    """Raised when a provider is asked to perform an operation that requires
    ontology-driven configuration (e.g. containment edge types) but no such
    configuration has been injected.

    Producers of this error: provider internals (e.g. FalkorDBProvider) when
    the ContextEngine has not yet called set_containment_edge_types() and no
    explicit env-var override is present.

    Consumers: API endpoints should translate this to HTTP 400 with a clear
    message about ontology configuration — never silently fall back to
    hardcoded defaults.
    """
    pass


class ProviderInputError(ValueError):
    """Raised when a write operation receives input that exceeds a
    provider-side limit before any I/O happens.

    Example: a single GraphNode property bag whose JSON encoding exceeds
    Spanner's 10 MiB cell limit. Without this guard, the offending row
    would fail the entire batched mutation atomically — poisoning every
    adjacent row in the same upsert. Catching at the boundary lets the
    API layer translate to HTTP 400 with a clear "row X is too large"
    message and let the caller retry without that row.

    Consumers: API endpoints should translate to HTTP 400.
    """
    pass


class GraphDataProvider(ABC):
    """
    Abstract interface for graph data providers.
    Enables swapping between Mock, FalkorDB, Neo4j, DataHub, etc.
    All methods must be async to prevent blocking the event loop.

    Implementations MUST bound every async I/O call with a per-operation
    deadline (e.g. via ``asyncio.wait_for``). The :class:`CircuitBreakerProxy`
    does not enforce deadlines on provider calls; deadlines are the
    provider's responsibility because only the provider knows the right
    granularity (a single query vs. a batched orchestration). Failure to
    comply will manifest as hung worker tasks during downstream incidents.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for debugging"""
        pass

    # ==========================================
    # Node Operations
    # ==========================================

    @abstractmethod
    async def get_node(self, urn: str) -> Optional[GraphNode]:
        pass

    @abstractmethod
    async def get_nodes(self, query: NodeQuery) -> List[GraphNode]:
        pass

    @abstractmethod
    async def search_nodes(self, query: str, limit: int = 10) -> List[GraphNode]:
        pass

    # ==========================================
    # Edge Operations
    # ==========================================

    @abstractmethod
    async def get_edges(self, query: EdgeQuery) -> List[GraphEdge]:
        pass

    # ==========================================
    # Containment Hierarchy
    # ==========================================

    @abstractmethod
    async def get_children(
        self,
        parent_urn: str,
        entity_types: Optional[List[str]] = None,
        edge_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
        sort_property: Optional[str] = "displayName",
        cursor: Optional[str] = None,
    ) -> List[GraphNode]:
        pass

    async def get_children_with_edges(
        self,
        parent_urn: str,
        edge_types: Optional[List[str]] = None,
        lineage_edge_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
        include_lineage_edges: bool = True,
        sort_property: Optional[str] = "displayName",
        cursor: Optional[str] = None,
    ) -> ChildrenWithEdgesResult:
        """Get children with containment and optionally lineage edges in one round-trip.

        Default implementation delegates to get_children + get_edges.
        Providers may override with an optimized single-query implementation.
        """
        from ..models.graph import EdgeQuery
        children = await self.get_children(
            parent_urn, edge_types=edge_types,
            search_query=search_query, offset=offset, limit=limit,
            sort_property=sort_property, cursor=cursor,
        )
        child_urns = [c.urn for c in children]
        all_urns = [parent_urn] + child_urns

        # Fetch containment edges between parent and children
        containment_edges: List[GraphEdge] = []
        lineage_edges: List[GraphEdge] = []
        if child_urns:
            edges = await self.get_edges(EdgeQuery(
                source_urns=all_urns, target_urns=all_urns, limit=len(all_urns) * 10,
            ))
            containment_types = set(t.upper() for t in (edge_types or []))
            lineage_filter = set(t.upper() for t in lineage_edge_types) if lineage_edge_types else None
            for e in edges:
                if e.edge_type.upper() in containment_types:
                    containment_edges.append(e)
                elif include_lineage_edges:
                    if lineage_filter is None or e.edge_type.upper() in lineage_filter:
                        lineage_edges.append(e)

        # We don't know total_children without a count query; approximate
        has_more = len(children) >= limit
        total = offset + len(children) + (1 if has_more else 0)

        next_cursor = children[-1].display_name if children and has_more else None
        return ChildrenWithEdgesResult(
            children=children,
            containmentEdges=containment_edges,
            lineageEdges=lineage_edges,
            totalChildren=total,
            hasMore=has_more,
            nextCursor=next_cursor,
        )

    @abstractmethod
    async def get_parent(self, child_urn: str) -> Optional[GraphNode]:
        pass

    async def get_top_level_or_orphan_nodes(
        self,
        *,
        root_entity_types: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        include_child_count: bool = True,
    ) -> TopLevelNodesResult:
        """Return instances that have no incoming containment edge.

        Definition: a node n is "top-level or orphan" iff there is no edge
        (n' -[:CONTAINMENT_TYPE]-> n) for any configured containment type.
        This is a purely structural predicate — it does NOT depend on the
        node's entity type. The result therefore mixes:
          - Instances of ontology root types (Domain, Platform, …)
          - Orphan instances of non-root types (a Table with no Schema parent,
            e.g. from a broken import)

        The UI distinguishes them via the root_type_count / orphan_count
        fields on TopLevelNodesResult.

        Pagination: cursor-based on display_name for stability under writes.
        Callers pass cursor=None for the first page and cursor=result.next_cursor
        for subsequent pages.

        Containment edge types are resolved from the ontology injected into
        the provider by ContextEngine. Providers MUST raise
        ProviderConfigurationError when no containment edge types are
        resolvable — do NOT silently default to hardcoded type names, as this
        breaks enterprise ontologies that use custom edge naming.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_top_level_or_orphan_nodes. "
            "Override this method to support the /nodes/top-level endpoint."
        )

    # ==========================================
    # Lineage Traversal
    # ==========================================

    @abstractmethod
    async def get_upstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        pass

    @abstractmethod
    async def get_downstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        pass

    @abstractmethod
    async def get_full_lineage(
        self,
        urn: str,
        upstream_depth: int,
        downstream_depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        pass

    @abstractmethod
    async def get_aggregated_edges_between(
        self,
        source_urns: List[str],
        target_urns: Optional[List[str]],
        granularity: Any,
        containment_edges: List[str],
        lineage_edges: List[str],
    ) -> Any:
        pass

    @abstractmethod
    async def get_trace_lineage(
        self,
        urn: str,
        direction: str,
        depth: int,
        containment_edges: List[str],
        lineage_edges: List[str],
    ) -> LineageResult:
        pass

    # ------------------------------------------------------------------ #
    # Trace v2 — Cypher-native, ontology-aware                           #
    # ------------------------------------------------------------------ #

    async def trace_at_level(
        self,
        urn: str,
        level: int,
        upstream_depth: int,
        downstream_depth: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        include_containment_edges: bool = False,
        include_inherited_lineage: bool = True,
    ) -> TraceResult:
        """Trace at a specific hierarchy level using AGGREGATED edges.

        Per-hop set-based BFS (orchestrated in Python, executed in Cypher).
        Returns nodes already at ``level`` plus AGGREGATED edges between
        them, scoped to ``upstream_depth`` / ``downstream_depth`` hops.

        Inherited lineage: if ``include_inherited_lineage=True`` and the
        focus has no AGGREGATED edges at the requested level, the trace
        anchors at the nearest containment ancestor that does, with
        ``isInherited=True`` in the result.

        Default implementation raises NotImplementedError — override in
        concrete providers (Neo4j, FalkorDB).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement trace_at_level. "
            "Required for the /trace/v2 endpoint."
        )

    async def expand_aggregated(
        self,
        source_urn: str,
        target_urn: str,
        next_level: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        use_raw_edges: bool = False,
        include_containment_edges: bool = False,
    ) -> TraceResult:
        """Drill into an AGGREGATED edge: return finer-level nodes + edges
        within (source_subtree × target_subtree) at ``next_level``.

        Set-based, no Cartesian: collect descendants at the target level
        for each anchor, then match edges between the two URN sets.

        When ``use_raw_edges=True`` (typically for the finest level where
        AGGREGATED == raw lineage), the implementation skips AGGREGATED
        and reads raw lineage edges directly.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement expand_aggregated. "
            "Required for the /trace/expand endpoint."
        )

    # ==========================================
    # Metadata Operations
    # ==========================================

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def get_schema_stats(self) -> GraphSchemaStats:
        pass

    @abstractmethod
    async def get_ontology_metadata(self) -> OntologyMetadata:
        pass

    @abstractmethod
    async def get_distinct_values(self, property_name: str) -> List[Any]:
        pass

    # ==========================================
    # Traversal & Filtering Extensions
    # ==========================================

    @abstractmethod
    async def get_ancestors(self, urn: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        pass

    @abstractmethod
    async def get_descendants(
        self,
        urn: str,
        depth: int = 5,
        entity_types: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[GraphNode]:
        pass

    @abstractmethod
    async def get_nodes_by_tag(self, tag: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        pass

    @abstractmethod
    async def get_nodes_by_layer(self, layer_id: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        pass

    # ==========================================
    # Write Operations
    # ==========================================

    @abstractmethod
    async def save_custom_graph(self, nodes: List[GraphNode], edges: List[GraphEdge]) -> bool:
        pass

    @abstractmethod
    async def create_node(self, node: GraphNode, containment_edge: Optional[GraphEdge] = None) -> bool:
        pass

    @abstractmethod
    async def create_edge(self, edge: GraphEdge) -> bool:
        """Persist a new edge. Returns True on success."""
        pass

    @abstractmethod
    async def update_edge(self, edge_id: str, properties: Dict[str, Any]) -> Optional[GraphEdge]:
        """Update mutable properties of an edge. Returns updated edge or None if not found."""
        pass

    @abstractmethod
    async def delete_edge(self, edge_id: str) -> bool:
        """Delete an edge by its ID. Returns True on success, False if not found."""
        pass

    # ==========================================
    # Optional Extension Methods
    # (concrete implementations are optional — default no-ops)
    # ==========================================

    # ==========================================
    # Projection / Materialization Lifecycle Hooks
    # (no-ops by default — providers override as needed)
    # ==========================================

    async def set_projection_mode(self, mode: str) -> None:
        """Switch the projection target for aggregation operations.

        ``mode`` is ``"in_source"`` (write aggregated edges to the source
        graph) or ``"dedicated"`` (write to a separate projection graph).
        Called by the aggregation worker per-job before materialization.
        """
        pass

    async def ensure_projections(self) -> None:
        """Set up projection infrastructure (indices, projection graphs, etc.)."""
        pass

    async def on_lineage_edge_written(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
        edge_type: str,
    ) -> None:
        """Called after a lineage edge is created/updated. Materializes AGGREGATED edges."""
        pass

    async def on_lineage_edge_deleted(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
    ) -> None:
        """Called after a lineage edge is removed. Decrements AGGREGATED edge weights."""
        pass

    async def on_containment_changed(self, urn: str) -> None:
        """Called when a node's containment (parent) changes. Rebuilds ancestor chains."""
        pass

    async def count_aggregated_edges(self) -> int:
        """Return the current count of materialized AGGREGATED edges.

        Used as the denominator for purge progress reporting — the
        purge handler reads this once before deletion starts so the UI
        can render a meaningful "X / total" indicator instead of "0 / 0"
        until the very last batch lands. Returns 0 for providers that
        don't materialise aggregated edges.
        """
        return 0

    async def purge_aggregated_edges(
        self,
        *,
        batch_size: int = 10_000,
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> int:
        """Remove ALL materialized AGGREGATED edges from the graph.

        Implementations should iterate the deletion in chunks of at most
        ``batch_size`` so a multi-million-edge purge produces visible
        progress (and so the operation cannot silently truncate at a
        single hard-coded LIMIT). After every batch, ``progress_callback``
        — when provided — is awaited with the running total of edges
        deleted so far. Returns the total deleted across all batches.
        """
        return 0

    async def discover_schema(self) -> Dict[str, Any]:
        """Introspect the database and return available labels, relationship
        types, property keys, and sample data.

        Used for schema mapping configuration when connecting to an external
        graph database with an unknown property schema.

        Returns
        -------
        dict
            Keys may include ``labels``, ``relationshipTypes``,
            ``labelDetails`` (per-label counts, property keys, samples),
            and ``suggestedMapping`` (a best-guess SchemaMapping dict).
            Returns empty dict by default.
        """
        return {}

    async def list_graphs(self) -> List[str]:
        """
        List named graph keys / databases available on this provider instance.
        FalkorDB: GRAPH.LIST  |  Neo4j: SHOW DATABASES
        Returns empty list by default.
        """
        return []

    async def close(self) -> None:
        """
        Release connection pool resources.
        Called by ProviderRegistry.evict() before removing a provider from cache.
        """
        pass
