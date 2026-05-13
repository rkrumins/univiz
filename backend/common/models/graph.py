from typing import List, Dict, Any, Optional, Set, Union
from enum import Enum
from pydantic import BaseModel, Field, validator

# ============================================
# Enums
# ============================================

class FilterOperator(str, Enum):
    EQUALS = 'equals'
    CONTAINS = 'contains'
    STARTS_WITH = 'startsWith'
    ENDS_WITH = 'endsWith'
    GT = 'gt'
    LT = 'lt'
    IN = 'in'
    NOT_IN = 'notIn'
    EXISTS = 'exists'
    NOT_EXISTS = 'notExists'

# ============================================
# Core Models
# ============================================

class GraphNode(BaseModel):
    urn: str
    entity_type: str = Field(alias="entityType")  # open string; validated against the active ontology
    display_name: str = Field(alias="displayName")
    qualified_name: Optional[str] = Field(None, alias="qualifiedName")
    description: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    layer_assignment: Optional[str] = Field(None, alias="layerAssignment")
    child_count: Optional[int] = Field(None, alias="childCount")
    source_system: Optional[str] = Field(None, alias="sourceSystem")
    last_synced_at: Optional[str] = Field(None, alias="lastSyncedAt")

    class Config:
        populate_by_name = True

class GraphEdge(BaseModel):
    id: str
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    edge_type: str = Field(alias="edgeType")  # open string; validated against the active ontology
    confidence: Optional[float] = None
    properties: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        populate_by_name = True

# ============================================
# Query Models
# ============================================

class PropertyFilter(BaseModel):
    field: str
    operator: FilterOperator
    value: Optional[Any] = None

class TagFilter(BaseModel):
    mode: str = "any"  # any, all, none
    tags: List[str]

class TextFilter(BaseModel):
    text: str
    operator: str = "contains"
    case_sensitive: bool = False

class NodeQuery(BaseModel):
    urns: Optional[List[str]] = None
    entity_types: Optional[List[str]] = Field(None, alias="entityTypes")  # open strings
    tags: Optional[List[str]] = None
    layer_id: Optional[str] = Field(None, alias="layerId")
    search_query: Optional[str] = Field(None, alias="searchQuery")
    property_filters: Optional[List[PropertyFilter]] = Field(None, alias="propertyFilters")
    tag_filters: Optional[TagFilter] = Field(None, alias="tagFilters")
    name_filter: Optional[TextFilter] = Field(None, alias="nameFilter")
    include_child_count: bool = Field(True, alias="includeChildCount")
    offset: Optional[int] = 0
    limit: Optional[int] = 100

    class Config:
        populate_by_name = True

class EdgeQuery(BaseModel):
    source_urns: Optional[List[str]] = Field(None, alias="sourceUrns")
    target_urns: Optional[List[str]] = Field(None, alias="targetUrns")
    any_urns: Optional[List[str]] = Field(None, alias="anyUrns")
    edge_types: Optional[List[str]] = Field(None, alias="edgeTypes")  # open strings
    min_confidence: Optional[float] = Field(None, alias="minConfidence")
    offset: Optional[int] = 0
    limit: Optional[int] = 100

    class Config:
        populate_by_name = True

# ============================================
# Result Models
# ============================================

class LineageResult(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    upstream_urns: Set[str] = Field(default_factory=set, alias="upstreamUrns")
    downstream_urns: Set[str] = Field(default_factory=set, alias="downstreamUrns")
    total_count: int = Field(alias="totalCount")
    has_more: bool = Field(alias="hasMore")
    aggregated_edges: Optional[Dict[str, Any]] = Field(None, alias="aggregatedEdges")

    class Config:
        populate_by_name = True

# ============================================
# Trace v2 Models — Cypher-native, ontology-aware lineage
# ============================================

class TraceFocus(BaseModel):
    """Identifies the focus node of a trace, with its resolved hierarchy level."""
    urn: str
    level: int
    entity_type: str = Field(alias="entityType")

    class Config:
        populate_by_name = True


class TraceRequest(BaseModel):
    """Skeleton-first trace request.

    Default behavior (body of just ``{"urn": "urn:..."}``) returns the
    top-level (Domain) skeleton — the set of level-0 entities that lineage
    flows through, plus the focus's containment ancestor chain. Drill-down
    is served by POST /trace/expand on a per-edge basis.

    Defaults were tuned for the skeleton: ``level=0`` (top), unbounded
    depth (the level-0 ontology is tiny, ~10s of nodes), and containment
    edges on so the canvas can place every node within its hierarchy.
    """
    urn: str
    direction: str = "both"  # upstream | downstream | both
    upstream_depth: int = Field(99, alias="upstreamDepth", ge=0)
    downstream_depth: int = Field(99, alias="downstreamDepth", ge=0)
    # 0      = top-level Domain skeleton (DEFAULT — skeleton-first)
    # int    = literal level
    # str    = entity-type-id ("dataset"); resolved to that type's level
    # "auto" = legacy peer-rollup; the engine clamps to 0 in get_trace_v2
    level: Union[str, int] = 0
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    # Default True: the canvas layer-assignment HARD RULE (children inherit
    # parent's layer) needs the containment chain in the response, or deep
    # nodes orphan. See memory/feedback_trace_v2_safety_nets.md.
    include_containment_edges: bool = Field(True, alias="includeContainmentEdges")
    include_inherited_lineage: bool = Field(True, alias="includeInheritedLineage")
    # Server-side invariant flag. When True (default), every returned node N
    # has every containment ancestor of N (up to a level-0 root) present in
    # the response. Reserved for future opt-out by non-canvas tooling.
    include_ancestor_chain: bool = Field(True, alias="includeAncestorChain")

    class Config:
        populate_by_name = True


class ExpandRequest(BaseModel):
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    next_level: Union[str, int] = Field(alias="nextLevel")
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    include_containment_edges: bool = Field(True, alias="includeContainmentEdges")

    class Config:
        populate_by_name = True


# V2 alias — distinguishes the skeleton-first expand contract from the
# legacy ExpandRequest. Same shape today; kept as a distinct symbol so the
# API and engine signatures advertise V2 semantics.
class TraceExpandRequest(ExpandRequest):
    """V2 expand: stateless drill-down. The (source_urn, target_urn, next_level)
    triple uniquely identifies the aggregated edge being expanded. No
    server-side session is required — the response invariant guarantees
    that every returned node's parent is either already-visible (from the
    originating /trace) or present in this response."""
    # Optional informational session ID — surfaced in error envelopes for
    # client correlation. NOT used to look up server state (no session
    # exists in Phase 1). Reserved for Phase 2 (Team B).
    trace_session_id: Optional[str] = Field(None, alias="traceSessionId")


class TraceExpandPair(BaseModel):
    """One aggregated-edge identifier inside a batch expand request.

    The full per-edge expand contract (lineage_edge_types, include_containment_edges)
    is hoisted onto the batch request so every pair shares the same flags —
    this matches how the frontend issues these calls in practice (one trace
    session uses one configuration)."""
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    next_level: Union[str, int] = Field(alias="nextLevel")

    class Config:
        populate_by_name = True


class TraceExpandBatchRequest(BaseModel):
    """Batched drill-down. Replaces N concurrent POSTs to /trace/expand with
    one request. The server fans out concurrently and merges results by id
    so the response is a deduplicated TraceDelta."""
    pairs: List[TraceExpandPair]
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    include_containment_edges: bool = Field(True, alias="includeContainmentEdges")
    trace_session_id: Optional[str] = Field(None, alias="traceSessionId")

    class Config:
        populate_by_name = True


class MegaNodeInfo(BaseModel):
    """A node whose AGGREGATED out-degree exceeded TRACE_DEGREE_CAP.

    Frontend uses ``total - shown`` to render a "+N more" chip; clicking
    the chip re-issues /trace/expand with a higher topN for that node only.
    """
    urn: str
    shown: int
    total: int
    direction: str = "downstream"  # upstream | downstream

    class Config:
        populate_by_name = True


class TraceMeta(BaseModel):
    """Per-trace metadata. Surfaces what the server did, why, and any
    truncation the client should reflect in UI."""
    regime: str = "skeleton"  # "skeleton" | "expand"
    effective_level: int = Field(0, alias="effectiveLevel")
    truncation_reason: Optional[str] = Field(None, alias="truncationReason")
    # Reasons (kept inline for grep):
    #   "max_nodes" | "timeout" | "degree_cap" | "cycle_detected" | "orphan"
    # Cold-start (AGGREGATED edges not level-stamped) is NOT a truncation:
    # the trace falls back to a label-scan path and returns correct (slower)
    # results. See `_check_levels_backfilled` in falkordb_provider.py.
    cypher_ms: int = Field(0, alias="cypherMs")
    node_count: int = Field(0, alias="nodeCount")
    edge_count: int = Field(0, alias="edgeCount")
    # When orphan-fallback fires, the highest level actually reached.
    fallback_level: Optional[int] = Field(None, alias="fallbackLevel")
    # One entry per node that hit TRACE_DEGREE_CAP.
    mega_nodes: List[MegaNodeInfo] = Field(default_factory=list, alias="megaNodes")
    # Trace session ID — opaque correlation ID, not used to look up state
    # in Phase 1. Surfaced for telemetry and frontend correlation.
    trace_session_id: Optional[str] = Field(None, alias="traceSessionId")
    ontology_digest: Optional[str] = Field(None, alias="ontologyDigest")
    # True when some AGGREGATED edges carry a stale or missing levelDigest
    # (ontology drifted since they were stamped, or backfill hasn't run).
    # Results are still correct — the trace falls back to the label-scan
    # path for those edges. UI can show a "stamps refreshing…" hint and
    # the next run of backfill_aggregated_levels.py will clear the flag.
    stale_levels: bool = Field(False, alias="staleLevels")

    class Config:
        populate_by_name = True


class TraceResult(BaseModel):
    """Legacy trace result. New code should use TraceResultV2 which adds
    ``meta``. The ``truncation_reason`` field on this model is preserved
    for backward compatibility — V2 callers should read ``meta.truncationReason``.

    INVARIANT (enforced by engine.get_trace_v2):
      For every node N in ``nodes``, every containment ancestor of N up to
      a top-level (level-0) entity is also present in ``nodes``. Required
      by ``useLayerAssignment`` in ContextViewCanvas — do not remove.
    """
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    containment_edges: List[GraphEdge] = Field(default_factory=list, alias="containmentEdges")
    upstream_urns: Set[str] = Field(default_factory=set, alias="upstreamUrns")
    downstream_urns: Set[str] = Field(default_factory=set, alias="downstreamUrns")
    focus: TraceFocus
    effective_level: int = Field(alias="effectiveLevel")
    is_inherited: bool = Field(False, alias="isInherited")
    inherited_from_urn: Optional[str] = Field(None, alias="inheritedFromUrn")
    truncated: bool = False
    # "max_nodes" | "timeout" | "degree_cap" | "cycle_detected" | "orphan" | None
    truncation_reason: Optional[str] = Field(None, alias="truncationReason")

    class Config:
        populate_by_name = True


class TraceResultV2(TraceResult):
    """Skeleton-first trace result. Adds ``meta`` for regime/timing/truncation.

    Same invariant as TraceResult: every node's containment ancestors are
    present in ``nodes`` up to a level-0 root.
    """
    meta: TraceMeta = Field(default_factory=TraceMeta)


class TraceDelta(TraceResultV2):
    """Response to POST /trace/expand. Same shape as TraceResultV2 with
    ``meta.regime == "expand"``. Reserved for Phase 2 (Team B) to switch
    to a structural delta (addNodes/addEdges/removeNodes/removeEdges)."""


class ContainmentResult(BaseModel):
    parent: Optional[GraphNode]
    children: List[GraphNode]
    has_nested_children: bool = Field(alias="hasNestedChildren")

    class Config:
        populate_by_name = True

class ChildrenWithEdgesResult(BaseModel):
    """Single round-trip result for children + their edges."""
    children: List[GraphNode]
    containment_edges: List[GraphEdge] = Field(default_factory=list, alias="containmentEdges")
    lineage_edges: List[GraphEdge] = Field(default_factory=list, alias="lineageEdges")
    total_children: int = Field(alias="totalChildren")
    has_more: bool = Field(alias="hasMore")
    next_cursor: Optional[str] = Field(None, alias="nextCursor")

    class Config:
        populate_by_name = True


class TopLevelNodesResult(BaseModel):
    """Paginated list of instances that have no incoming containment edge.

    "Top-level" is defined structurally: a node n is top-level iff there is no
    edge of any configured containment type whose target is n. This includes:
      - Ontology root instances (Domain, Platform, etc.)
      - Orphan instances of non-root types (e.g. a Table with no parent schema)

    Callers use root_type_count / orphan_count to distinguish the two classes
    in UI (e.g. an "orphan" badge in the wizard tree).
    """
    nodes: List[GraphNode]
    total_count: int = Field(alias="totalCount")
    has_more: bool = Field(alias="hasMore")
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
    root_type_count: int = Field(0, alias="rootTypeCount")
    orphan_count: int = Field(0, alias="orphanCount")

    class Config:
        populate_by_name = True


# ============================================
# Introspection Models
# ============================================

class EntityTypeSummary(BaseModel):
    id: str
    name: str
    count: int
    icon: Optional[str] = None
    color: Optional[str] = None
    sample_names: List[str] = Field(default_factory=list, alias="sampleNames")

class EdgeTypeSummary(BaseModel):
    id: str
    name: str
    count: int
    source_types: List[str] = Field(default_factory=list, alias="sourceTypes")
    target_types: List[str] = Field(default_factory=list, alias="targetTypes")

class TagSummary(BaseModel):
    tag: str
    count: int
    entity_types: List[str] = Field(default_factory=list, alias="entityTypes")

class GraphSchemaStats(BaseModel):
    total_nodes: int = Field(alias="totalNodes")
    total_edges: int = Field(alias="totalEdges")
    entity_type_stats: List[EntityTypeSummary] = Field(default_factory=list, alias="entityTypeStats")
    edge_type_stats: List[EdgeTypeSummary] = Field(default_factory=list, alias="edgeTypeStats")
    tag_stats: List[TagSummary] = Field(default_factory=list, alias="tagStats")

    class Config:
        populate_by_name = True

# ============================================
# Ontology Metadata Models
# ============================================

class EdgeTypeMetadata(BaseModel):
    is_containment: bool = Field(alias="isContainment")
    is_lineage: bool = Field(default=False, alias="isLineage")
    direction: str  # 'parent-to-child', 'child-to-parent', 'source-to-target', 'bidirectional'
    category: str = Field(default="association")  # 'structural', 'flow', 'metadata', 'association'
    description: Optional[str] = None

    class Config:
        populate_by_name = True

class EntityTypeHierarchy(BaseModel):
    can_contain: List[str] = Field(default_factory=list, alias="canContain")
    can_be_contained_by: List[str] = Field(default_factory=list, alias="canBeContainedBy")

    class Config:
        populate_by_name = True

class OntologyMetadata(BaseModel):
    """Flat traversal metadata projected from ResolvedOntology for API callers."""
    containment_edge_types: List[str] = Field(alias="containmentEdgeTypes")
    lineage_edge_types: List[str] = Field(default_factory=list, alias="lineageEdgeTypes")
    edge_type_metadata: Dict[str, EdgeTypeMetadata] = Field(alias="edgeTypeMetadata")
    entity_type_hierarchy: Dict[str, EntityTypeHierarchy] = Field(alias="entityTypeHierarchy")
    root_entity_types: List[str] = Field(default_factory=list, alias="rootEntityTypes")

    class Config:
        populate_by_name = True

# ============================================
# Schema Definition Models
# ============================================

class FieldSchema(BaseModel):
    id: str
    name: str
    type: str
    required: bool = False
    show_in_node: bool = Field(default=True, alias="showInNode")
    show_in_panel: bool = Field(default=True, alias="showInPanel")
    show_in_tooltip: bool = Field(default=False, alias="showInTooltip")
    display_order: int = Field(default=0, alias="displayOrder")

    class Config:
        populate_by_name = True

class EntityVisualSchema(BaseModel):
    icon: str = "Box"
    color: str = "#6366f1"
    shape: str = "rounded"
    size: str = "md"
    border_style: str = Field(default="solid", alias="borderStyle")
    show_in_minimap: bool = Field(default=True, alias="showInMinimap")

    class Config:
        populate_by_name = True

class EntityHierarchySchema(BaseModel):
    level: int = 0
    can_contain: List[str] = Field(default_factory=list, alias="canContain")
    can_be_contained_by: List[str] = Field(default_factory=list, alias="canBeContainedBy")
    default_expanded: bool = Field(default=False, alias="defaultExpanded")

    class Config:
        populate_by_name = True

class EntityBehaviorSchema(BaseModel):
    selectable: bool = True
    draggable: bool = True
    expandable: bool = True
    traceable: bool = True
    click_action: str = Field(default="select", alias="clickAction")
    double_click_action: str = Field(default="expand", alias="doubleClickAction")

    class Config:
        populate_by_name = True

class EntityTypeDefinition(BaseModel):
    id: str
    name: str
    plural_name: str = Field(alias="pluralName")
    description: Optional[str] = None
    visual: EntityVisualSchema
    fields: List[FieldSchema] = Field(default_factory=list)
    hierarchy: EntityHierarchySchema
    behavior: EntityBehaviorSchema

    class Config:
        populate_by_name = True

class RelationshipVisualSchema(BaseModel):
    stroke_color: str = Field(default="#6366f1", alias="strokeColor")
    stroke_width: int = Field(default=2, alias="strokeWidth")
    stroke_style: str = Field(default="solid", alias="strokeStyle")
    animated: bool = True
    animation_speed: str = Field(default="normal", alias="animationSpeed")
    arrow_type: str = Field(default="arrow", alias="arrowType")
    curve_type: str = Field(default="bezier", alias="curveType")

    class Config:
        populate_by_name = True

class RelationshipTypeDefinition(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source_types: List[str] = Field(default_factory=list, alias="sourceTypes")
    target_types: List[str] = Field(default_factory=list, alias="targetTypes")
    visual: RelationshipVisualSchema
    bidirectional: bool = False
    show_label: bool = Field(default=False, alias="showLabel")
    is_containment: bool = Field(default=False, alias="isContainment")
    is_lineage: bool = Field(default=False, alias="isLineage")
    category: str = Field(default="association", alias="category")

    class Config:
        populate_by_name = True

class GraphSchema(BaseModel):
    """Frontend schema-store API contract serialized from resolved ontology."""
    version: str = "1.0.0"
    entity_types: List[EntityTypeDefinition] = Field(alias="entityTypes")
    relationship_types: List[RelationshipTypeDefinition] = Field(alias="relationshipTypes")
    root_entity_types: List[str] = Field(default_factory=list, alias="rootEntityTypes")
    containment_edge_types: List[str] = Field(default_factory=list, alias="containmentEdgeTypes")
    lineage_edge_types: List[str] = Field(default_factory=list, alias="lineageEdgeTypes")
    # Stable SHA-256 digest of the underlying OntologyMetadata projection.
    # Byte-identical to the value stamped onto ViewORM.ontology_digest when
    # a view is saved — the ViewWizard compares these two to decide whether
    # to render <OntologyDriftBanner>. Null when the digest couldn't be
    # computed (provider down, unresolvable ontology); the wizard silently
    # skips drift detection in that case instead of crying wolf.
    ontology_digest: Optional[str] = Field(default=None, alias="ontologyDigest")

    class Config:
        populate_by_name = True

# ============================================
# Aggregated Edge Models
# ============================================

class AggregatedEdgeRequest(BaseModel):
    source_urns: List[str] = Field(alias="sourceUrns")
    target_urns: Optional[List[str]] = Field(None, alias="targetUrns")
    granularity: Optional[str] = None  # entity type ID from the active ontology, e.g. "dataset", "term"; None = no aggregation
    include_edge_types: Optional[List[str]] = Field(None, alias="includeEdgeTypes")  # open strings
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    containment_edge_types: Optional[List[str]] = Field(None, alias="containmentEdgeTypes")

    class Config:
        populate_by_name = True

class AggregatedEdgeInfo(BaseModel):
    id: str
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    edge_count: int = Field(alias="edgeCount")
    edge_types: List[str] = Field(default_factory=list, alias="edgeTypes")
    confidence: float = 1.0
    source_edge_ids: List[str] = Field(default_factory=list, alias="sourceEdgeIds")

    class Config:
        populate_by_name = True

class AggregatedEdgeResult(BaseModel):
    aggregated_edges: List[AggregatedEdgeInfo] = Field(alias="aggregatedEdges")
    total_source_edges: int = Field(alias="totalSourceEdges")
    truncated: bool = False
    last_materialized_at: Optional[str] = Field(default=None, alias="lastMaterializedAt")
    materialization_triggered: bool = Field(default=False, alias="materializationTriggered")

    class Config:
        populate_by_name = True

# ============================================
# Node Creation Models
# ============================================

class CreateNodeRequest(BaseModel):
    entity_type: str = Field(alias="entityType")  # open string
    display_name: str = Field(alias="displayName")
    parent_urn: Optional[str] = Field(None, alias="parentUrn")
    properties: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True

class CreateNodeResult(BaseModel):
    node: GraphNode
    containment_edge: Optional[GraphEdge] = Field(None, alias="containmentEdge")
    success: bool = True
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True


# ============================================
# Edge Mutation Models
# ============================================

class CreateEdgeRequest(BaseModel):
    """Create a directed edge between two existing nodes."""
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    edge_type: str = Field(alias="edgeType")
    properties: Dict[str, Any] = Field(default_factory=dict)
    # Idempotency key — if supplied and a matching edge already exists, return it unchanged.
    idempotency_key: Optional[str] = Field(None, alias="idempotencyKey")

    class Config:
        populate_by_name = True


class UpdateEdgeRequest(BaseModel):
    """Update mutable properties of an existing edge. edge_type is immutable."""
    properties: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        populate_by_name = True


class EdgeMutationResult(BaseModel):
    edge: Optional[GraphEdge] = None
    success: bool = True
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True


# ============================================
# Batch Command Models
# ============================================

class BatchCommandOp(str):
    CREATE_NODE = "create_node"
    UPDATE_NODE = "update_node"
    DELETE_NODE = "delete_node"
    CREATE_EDGE = "create_edge"
    UPDATE_EDGE = "update_edge"
    DELETE_EDGE = "delete_edge"


class BatchCommand(BaseModel):
    """A single operation within a batch request."""
    op: str = Field(..., description="create_node | update_node | delete_node | create_edge | update_edge | delete_edge")
    ref: Optional[str] = Field(None, description="Client-side reference for correlating responses.")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Op-specific payload (matches single-op request body).")

    class Config:
        populate_by_name = True


class BatchCommandRequest(BaseModel):
    """Batch of graph mutation commands executed atomically."""
    commands: List[BatchCommand]
    # If True, abort the entire batch on the first failure (default).
    # If False, continue and collect all results.
    fail_fast: bool = Field(True, alias="failFast")

    class Config:
        populate_by_name = True


class BatchCommandResult(BaseModel):
    """Result for a single command within the batch."""
    ref: Optional[str] = None
    op: str
    success: bool
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    # For create ops: the created resource
    created_urn: Optional[str] = Field(None, alias="createdUrn")
    created_edge_id: Optional[str] = Field(None, alias="createdEdgeId")

    class Config:
        populate_by_name = True


class BatchResponse(BaseModel):
    results: List[BatchCommandResult]
    total: int
    succeeded: int
    failed: int

    class Config:
        populate_by_name = True
