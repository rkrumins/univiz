/**
 * GraphDataProvider - Abstract interface for graph data sources
 * 
 * This interface abstracts the underlying graph database, enabling
 * easy integration with FalkorDB, Neo4j, DataHub GraphQL, or any
 * other graph data source.
 */

import { LogicalNodeConfig, LayerAssignmentRuleConfig, RuleCondition, ScopeFilterConfig, EntityAssignmentConfig } from '../types/schema'
import type { TraceMeta } from '@/services/traceApi'

// ============================================
// URN Types (DataHub Compatible)
// ============================================

/**
 * Unique Resource Name following DataHub convention
 * Examples:
 * - urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.revenue,PROD)
 * - urn:li:schemaField:(urn:li:dataset:...,amount)
 * - urn:li:dataJob:(urn:li:dataFlow:...,transform_revenue)
 */
export type URN = string

/**
 * Entity type identifier — any string ID defined in the active ontology.
 * Custom ontologies (Glossary, PII, etc.) produce type IDs that are not
 * enumerable at compile time, so this is intentionally `string` rather
 * than a closed union. Use the schema store or `useEntityTypes()` hook to
 * get the list of available types at runtime.
 */
export type EntityType = string

/**
 * Edge type identifier -- any string defined in the active ontology.
 * Custom ontologies produce edge types that are not enumerable at compile time.
 * Use the schema store or `useRelationshipTypes()` to get available types at runtime.
 */
export type EdgeType = string

// ============================================
// Graph Node & Edge
// ============================================

/**
 * Normalized node representation from any graph source
 */
export interface GraphNode {
    /** Unique identifier (URN format preferred) */
    urn: URN

    /** Entity type for rendering and behavior */
    entityType: EntityType

    /** Human-readable name */
    displayName: string

    /** Technical name/path */
    qualifiedName?: string

    /** Optional description */
    description?: string

    /** Arbitrary properties from source system */
    properties: Record<string, unknown>

    /** Tags for classification and layer assignment */
    tags?: string[]

    /** Resolved layer assignment (if applicable) */
    layerAssignment?: string

    /** Count of contained children (when collapsed) */
    childCount?: number

    /** Source system identifier */
    sourceSystem?: string

    /** Last sync timestamp */
    lastSyncedAt?: string
}

/**
 * Relationship between two graph nodes
 */
export interface GraphEdge {
    /** Unique edge identifier */
    id: string

    /** Source node URN */
    sourceUrn: URN

    /** Target node URN */
    targetUrn: URN

    /** Relationship type */
    edgeType: EdgeType

    /** Confidence score for derived relationships (0.0 - 1.0) */
    confidence?: number

    /** Additional edge properties */
    properties?: Record<string, unknown>
}

// ============================================
// Introspection Types
// ============================================

export interface EntityTypeSummary {
    id: string
    name: string
    count: number
    icon?: string
    color?: string
    sampleNames: string[]
}

export interface EdgeTypeSummary {
    id: string
    name: string
    count: number
    sourceTypes: string[]
    targetTypes: string[]
}

export interface TagSummary {
    tag: string
    count: number
    entityTypes: string[]
}

export interface EdgeTypeMetadata {
    isContainment: boolean
    isLineage: boolean
    direction: 'parent-to-child' | 'child-to-parent' | 'source-to-target' | 'bidirectional'
    category: 'structural' | 'flow' | 'metadata' | 'association'
    description?: string
}

export interface EntityTypeHierarchy {
    canContain: string[]
    canBeContainedBy: string[]
}

export interface OntologyMetadata {
    containmentEdgeTypes: string[]
    lineageEdgeTypes: string[]
    edgeTypeMetadata: Record<string, EdgeTypeMetadata>
    entityTypeHierarchy: Record<string, EntityTypeHierarchy>
    rootEntityTypes: string[]
}

// ============================================
// Schema Definition Types (for dynamic loading)
// ============================================

export interface FieldSchema {
    id: string
    name: string
    type: string
    required: boolean
    showInNode: boolean
    showInPanel: boolean
    showInTooltip: boolean
    displayOrder: number
}

export interface EntityVisualSchema {
    icon: string
    color: string
    shape: string
    size: string
    borderStyle: string
    showInMinimap: boolean
}

export interface EntityHierarchySchema {
    level: number
    canContain: string[]
    canBeContainedBy: string[]
    defaultExpanded: boolean
}

export interface EntityBehaviorSchema {
    selectable: boolean
    draggable: boolean
    expandable: boolean
    traceable: boolean
    clickAction: string
    doubleClickAction: string
}

export interface EntityTypeDefinition {
    id: string
    name: string
    pluralName: string
    description?: string
    visual: EntityVisualSchema
    fields: FieldSchema[]
    hierarchy: EntityHierarchySchema
    behavior: EntityBehaviorSchema
}

export interface RelationshipVisualSchema {
    strokeColor: string
    strokeWidth: number
    strokeStyle: string
    animated: boolean
    animationSpeed: string
    arrowType: string
    curveType: string
}

export interface RelationshipTypeDefinition {
    id: string
    name: string
    description?: string
    sourceTypes: string[]
    targetTypes: string[]
    visual: RelationshipVisualSchema
    bidirectional: boolean
    showLabel: boolean
    isContainment: boolean
    isLineage: boolean
    category: 'structural' | 'flow' | 'metadata' | 'association'
}

export interface GraphSchema {
    version: string
    entityTypes: EntityTypeDefinition[]
    relationshipTypes: RelationshipTypeDefinition[]
    rootEntityTypes: string[]
    containmentEdgeTypes: string[]
    lineageEdgeTypes: string[]
    /**
     * SHA-256 digest of the underlying OntologyMetadata projection.
     * Byte-identical to `ViewORM.ontology_digest` at save time — the
     * ViewWizard compares the view's stored digest against this value
     * to decide whether to render <OntologyDriftBanner>. Null when the
     * backend couldn't resolve the ontology; drift check no-ops in
     * that case.
     */
    ontologyDigest?: string | null
}

// ============================================
// Aggregated Edge Types
// ============================================

export interface AggregatedEdgeRequest {
    sourceUrns: string[]
    targetUrns?: string[]
    /** Entity type ID to aggregate to (e.g. "dataset", "term"). null = no aggregation. */
    granularity: string | null
    includeEdgeTypes?: string[]
    lineageEdgeTypes?: string[]
    containmentEdgeTypes?: string[]
}

export interface AggregatedEdgeInfo {
    id: string
    sourceUrn: string
    targetUrn: string
    edgeCount: number
    edgeTypes: string[]
    confidence: number
    sourceEdgeIds: string[]
}

export interface AggregatedEdgeResult {
    aggregatedEdges: AggregatedEdgeInfo[]
    totalSourceEdges: number
    truncated?: boolean
    lastMaterializedAt?: string | null
    materializationTriggered?: boolean
}

// ============================================
// Node Creation Types
// ============================================

export interface CreateNodeRequest {
    entityType: EntityType
    displayName: string
    parentUrn?: string
    properties: Record<string, unknown>
    tags: string[]
}

export interface CreateNodeResult {
    node: GraphNode | null
    containmentEdge: GraphEdge | null
    success: boolean
    error?: string
}

export interface GraphSchemaStats {
    totalNodes: number
    totalEdges: number
    entityTypeStats: EntityTypeSummary[]
    edgeTypeStats: EdgeTypeSummary[]
    tagStats: TagSummary[]
}

// ============================================
// Query Types
// ============================================

export type FilterOperator =
    | 'equals' | 'contains' | 'startsWith' | 'endsWith'
    | 'gt' | 'lt' | 'in' | 'notIn' | 'exists' | 'notExists'

export interface PropertyFilter {
    field: string
    operator: FilterOperator
    value: unknown
}

export interface DescendantPreviewQuery {
    nameSubstring?: string
    entityTypes?: EntityType[]
    propertyFilter?: PropertyFilter
    sampleLimit?: number
    hardCap?: number
}

export interface DescendantPreviewResult {
    total: number
    sample: GraphNode[]
    truncated: boolean
}

export interface TagFilter {
    mode: 'any' | 'all' | 'none'
    tags: string[]
}

export interface TextFilter {
    text: string
    operator: 'contains' | 'startsWith' | 'endsWith' | 'equals'
    caseSensitive?: boolean
}

export interface NodeQuery {
    /** Filter by URNs */
    urns?: URN[]

    /** Filter by entity types */
    entityTypes?: EntityType[]

    /** Filter by tags */
    tags?: string[]

    /** Filter by layer assignment */
    layerId?: string

    /** Full-text search query */
    searchQuery?: string

    /** Advanced Property Filters */
    propertyFilters?: PropertyFilter[]

    /** Advanced Tag Filters */
    tagFilters?: TagFilter

    /** Specific Name/Text Filter */
    nameFilter?: TextFilter

    /** Pagination offset */
    offset?: number

    /** Pagination limit */
    limit?: number
}

export interface EdgeQuery {
    /** Filter by source URNs */
    sourceUrns?: URN[]

    /** Filter by target URNs */
    targetUrns?: URN[]

    /** Include edges where URNs appear as source OR target */
    anyUrns?: URN[]

    /** Filter by edge types */
    edgeTypes?: EdgeType[]

    /** Minimum confidence score */
    minConfidence?: number

    /** Pagination offset */
    offset?: number

    /** Pagination limit */
    limit?: number
}

export interface LineageResult {
    /** Nodes in the lineage path */
    nodes: GraphNode[]

    /** Edges connecting the nodes */
    edges: GraphEdge[]

    /** URNs of upstream nodes (relative to starting point) */
    upstreamUrns: Set<URN>

    /** URNs of downstream nodes (relative to starting point) */
    downstreamUrns: Set<URN>

    /** Total count (may exceed returned nodes due to pagination) */
    totalCount: number

    /** Whether more results are available */
    hasMore: boolean

    /** Aggregated edges metadata (for progressive disclosure) */
    aggregatedEdges?: Record<string, unknown>

    /** URN of parent entity if lineage was inherited */
    inheritedFrom?: string
}

// ============================================
// Trace v2 — Cypher-native, ontology-aware lineage
// ============================================

export interface TraceV2Focus {
    urn: URN
    level: number
    entityType: string
}

export interface TraceV2Request {
    urn: URN
    direction?: 'upstream' | 'downstream' | 'both'
    upstreamDepth?: number
    downstreamDepth?: number
    /** "auto" = peer rollup at source's own level | int | entity-type-id */
    level?: 'auto' | number | string
    /** null = use all ontology lineage types */
    lineageEdgeTypes?: string[] | null
    includeContainmentEdges?: boolean
    includeInheritedLineage?: boolean
}

export interface ExpandAggregatedRequest {
    sourceUrn: URN
    targetUrn: URN
    nextLevel: number | string
    lineageEdgeTypes?: string[] | null
    includeContainmentEdges?: boolean
}

export interface ExpandAggregatedBatchRequest {
    /** One entry per aggregated-edge to drill into; all share the same config below. */
    pairs: Array<{
        sourceUrn: URN
        targetUrn: URN
        nextLevel: number | string
    }>
    lineageEdgeTypes?: string[] | null
    includeContainmentEdges?: boolean
}

export interface TraceV2Result {
    nodes: GraphNode[]
    edges: GraphEdge[]
    /** Containment edges between returned nodes (populated only when requested) */
    containmentEdges: GraphEdge[]
    upstreamUrns: Set<URN>
    downstreamUrns: Set<URN>
    focus: TraceV2Focus
    /** The integer hierarchy level the trace ran at (server-resolved) */
    effectiveLevel: number
    /** True if the trace anchored at an ancestor because the focus had no direct lineage */
    isInherited: boolean
    inheritedFromUrn?: string | null
    truncated: boolean
    /** "max_nodes" | "timeout" | null */
    truncationReason?: string | null
    /** Sidecar performance metadata (cache, regime, query latency) — present when emitted by the v2 envelope */
    meta?: TraceMeta
}

/**
 * Options for trace/lineage operations
 */
export interface TraceOptions {
    /** Include column-level lineage (default: true) */
    includeColumnLineage?: boolean

    /** Exclude containment edges for pure data lineage (default: true) */
    excludeContainmentEdges?: boolean

    /** Include inherited lineage from children (default: true) */
    includeInheritedLineage?: boolean

    /** Target entity type ID to aggregate to (e.g. "dataset", "term"). null = no aggregation. */
    granularity?: string | null

    /** Aggregate edges at granularity level (default: true) */
    aggregateEdges?: boolean

    /** Optional whitelist of lineage edge types to trace (default: all ontology lineage types) */
    lineageEdgeTypes?: string[]
}

export interface ContainmentResult {
    /** Parent node (null if querying root) */
    parent: GraphNode | null

    /** Direct children */
    children: GraphNode[]

    /** Whether children have their own children */
    hasNestedChildren: boolean
}

// ============================================
// Top-Level / Orphan Node Query Types
// ============================================

/**
 * Query for nodes that sit at the top of the containment hierarchy.
 *
 * Definition (enterprise-ready, ontology-agnostic):
 *   A node is "top-level" if there is NO incoming containment edge of any
 *   type defined in the active ontology. This covers both ontology roots
 *   (e.g. Domain entities that are always roots) AND orphan instances of
 *   non-root types (e.g. a Platform that was ingested without a Domain).
 *
 * Replaces the old "rootEntityTypes" heuristic which silently mis-classified
 * any ontology where the root type wasn't actually at the top, and couldn't
 * surface orphans at all.
 */
export interface TopLevelNodesQuery {
    /** Optional entity-type filter for the type-picker dropdown. */
    entityTypes?: EntityType[]
    /** Case-insensitive substring search over displayName/urn. */
    searchQuery?: string
    /** Page size. Backend clamps to 1..1000. */
    limit?: number
    /** Keyset cursor: the displayName of the last node returned on the
     *  previous page. `null`/undefined starts from the beginning. */
    cursor?: string | null
    /** When true (default), each returned node has `childCount` populated. */
    includeChildCount?: boolean
}

export interface TopLevelNodesResult {
    nodes: GraphNode[]
    /** Total count across all pages (diagnostic — NOT nodes.length). */
    totalCount: number
    hasMore: boolean
    nextCursor: string | null
    /** How many of `totalCount` are ontology-root instances. */
    rootTypeCount: number
    /** How many are orphans of non-root types (missing containment in-edge). */
    orphanCount: number
}

// ============================================
// Provider Interface
// ============================================

/**
 * Abstract interface for graph data providers
 * 
 * Implementations:
 * - FalkorDBProvider: Cypher queries to FalkorDB/Neo4j
 * - DataHubProvider: GraphQL queries to DataHub
 */
export interface GraphDataProvider {
    /** Provider name for debugging */
    readonly name: string

    // ==========================================
    // Node Operations
    // ==========================================

    /**
     * Get a single node by URN
     */
    getNode(urn: URN): Promise<GraphNode | null>

    /**
     * Query multiple nodes
     */
    getNodes(query: NodeQuery): Promise<GraphNode[]>

    /**
     * Search nodes by text query
     */
    searchNodes(query: string, limit?: number): Promise<GraphNode[]>

    // ==========================================
    // Edge Operations
    // ==========================================

    /**
     * Query edges matching criteria
     */
    getEdges(query: EdgeQuery): Promise<GraphEdge[]>

    /**
     * Get edges where BOTH source and target are in the provided URN set.
     * Server-side filtered — only returns internal edges between loaded nodes.
     */
    getEdgesBetween(urns: URN[], edgeTypes?: string[], limit?: number): Promise<GraphEdge[]>

    // ==========================================
    // Containment Hierarchy (CONTAINS relationships)
    // ==========================================

    /**
     * Get direct children of a node
     * @param parentUrn - Parent node URN
     * @param options - Pagination and filtering options
     */
    getChildren(
        parentUrn: URN,
        options?: {
            entityTypes?: EntityType[]
            edgeTypes?: string[] // Custom edge types for containment
            searchQuery?: string
            offset?: number
            limit?: number
            sortProperty?: string | null // Node property to sort by (default: displayName, null = no sort)
            cursor?: string | null // Cursor for keyset pagination (displayName of last item)
        }
    ): Promise<GraphNode[]>

    /**
     * Get children with containment and lineage edges in a single round-trip.
     * Eliminates the need for separate getChildren + getEdgesBetween calls.
     */
    getChildrenWithEdges(
        parentUrn: URN,
        options?: {
            edgeTypes?: string[]
            lineageEdgeTypes?: string[]
            searchQuery?: string
            offset?: number
            limit?: number
            includeLineageEdges?: boolean
            sortProperty?: string | null // Node property to sort by (default: displayName, null = no sort)
            cursor?: string | null // Cursor for keyset pagination (displayName of last item)
        }
    ): Promise<{
        children: GraphNode[]
        containmentEdges: GraphEdge[]
        lineageEdges: GraphEdge[]
        totalChildren: number
        hasMore: boolean
        nextCursor?: string | null
    }>

    /**
     * Server-side preview of descendants under `parentUrn` matching the filter.
     * Used by the ViewWizard to show "this scoped rule will match N entities"
     * before authoring a LayerAssignmentRuleConfig. Filters mirror the
     * backend rule semantics so the preview count reflects what the
     * AssignmentEngine will actually resolve.
     */
    getDescendantsPreview(
        parentUrn: URN,
        query: DescendantPreviewQuery,
        options?: { edgeTypes?: string[] }
    ): Promise<DescendantPreviewResult>

    /**
     * Get parent of a node (inverse of CONTAINS)
     */
    getParent(childUrn: URN): Promise<GraphNode | null>

    /**
     * Get all ancestors up to root
     */
    getAncestors(urn: URN): Promise<GraphNode[]>

    /**
     * Get all descendants recursively
     * @param depth - Maximum depth (default: 10)
     */
    getDescendants(urn: URN, depth?: number): Promise<GraphNode[]>

    /**
     * Get containment context: parent + children matching optional search
     * Used for SearchChildrenPanel and similar UIs
     */
    getContainment?(params: { parentUrn: URN; searchQuery?: string; limit?: number }): Promise<ContainmentResult>

    /**
     * Get nodes sitting at the top of the containment hierarchy — every
     * node with NO incoming containment edge, regardless of entity type.
     * This is the ontology-agnostic replacement for "root-type" queries
     * and is what the ViewWizard's AssignmentStep uses to seed its tree.
     *
     * Backend MUST raise on empty containment-edge ontology: silently
     * returning everything (or hardcoding CONTAINS/BELONGS_TO) is the
     * behavior this method was introduced to replace.
     */
    getTopLevelNodes(query: TopLevelNodesQuery): Promise<TopLevelNodesResult>

    // ==========================================
    // Lineage Traversal
    // ==========================================

    /**
     * Get upstream lineage (data sources flowing INTO this entity)
     * @param urn - Starting entity URN
     * @param depth - How many hops upstream
     * @param options - Additional trace options
     */
    getUpstream(
        urn: URN,
        depth: number,
        options?: TraceOptions
    ): Promise<LineageResult>

    /**
     * Get downstream lineage (entities this data flows TO)
     * @param urn - Starting entity URN
     * @param depth - How many hops downstream
     * @param options - Additional trace options
     */
    getDownstream(
        urn: URN,
        depth: number,
        options?: TraceOptions
    ): Promise<LineageResult>

    /**
     * Get both upstream and downstream lineage
     * @param urn - Starting entity URN
     * @param upstreamDepth - How many hops upstream
     * @param downstreamDepth - How many hops downstream
     * @param options - Additional trace options
     */
    getFullLineage(
        urn: URN,
        upstreamDepth: number,
        downstreamDepth: number,
        options?: TraceOptions
    ): Promise<LineageResult>

    /**
     * Trace v2 — Cypher-native, ontology-aware. Returns nodes already at
     * the requested hierarchy level (peer rollup) plus AGGREGATED edges.
     * Tracing from a Domain doesn't explode to Columns.
     *
     * Default `level: "auto"` resolves server-side to the source node's
     * own `hierarchy.level`. Hard caps (max_nodes, timeout_ms) are server
     * config — when tripped, response carries `truncated: true`.
     */
    traceAtLevel?(request: TraceV2Request): Promise<TraceV2Result>

    /**
     * Drill into an AGGREGATED edge: return finer-level nodes + edges
     * within (source-subtree × target-subtree) at `nextLevel`.
     */
    expandAggregated?(request: ExpandAggregatedRequest): Promise<TraceV2Result>

    /**
     * Batched drill-down. The frontend collects all incident AGGREGATED
     * edges of an expanding traced node into a single backend call —
     * one HTTP round trip instead of one-per-edge. Server fans out and
     * returns a merged, deduplicated TraceV2Result.
     */
    expandAggregatedBatch?(request: ExpandAggregatedBatchRequest): Promise<TraceV2Result>

    // ==========================================
    // Layer/Classification Queries
    // ==========================================

    /**
     * Get nodes assigned to a specific layer
     * Layer assignment can be by tag, entity type, or explicit mapping
     */
    getNodesByLayer(layerId: string): Promise<GraphNode[]>

    /**
     * Get nodes with a specific tag
     */
    getNodesByTag(tag: string): Promise<GraphNode[]>

    // ==========================================
    // Metadata Operations
    // ==========================================

    /**
     * Get available entity types in the graph
     */
    getEntityTypes(): Promise<EntityType[]>

    /**
     * Get all unique tags in the graph
     */
    getTags(): Promise<string[]>

    /**
     * Get graph statistics
     */
    getStats(): Promise<{
        nodeCount: number
        edgeCount: number
        entityTypeCounts: Record<EntityType, number>
    }>

    /**
     * Get detailed graph schema statistics
     */
    getSchemaStats(): Promise<GraphSchemaStats>

    /**
     * Get ontology metadata including containment edge types and entity hierarchies
     */
    getOntologyMetadata(): Promise<OntologyMetadata>

    // ==========================================
    // Assignment Operations
    // ==========================================

    /**
     * Compute layer assignments for the graph (server-side)
     */
    computeLayerAssignments(request: LayerAssignmentRequest): Promise<LayerAssignmentResult>

    // ==========================================
    // Schema Operations (Dynamic Schema Loading)
    // ==========================================

    /**
     * Get complete graph schema from backend.
     * @param dataSourceId - Optional override. When provided, returns the schema for
     *   that specific data source (and its assigned ontology) rather than the default.
     */
    getFullSchema(dataSourceId?: string): Promise<GraphSchema>

    // ==========================================
    // Aggregated Edge Operations
    // ==========================================

    /**
     * Get aggregated edges between containers at a specified granularity
     * Enables progressive edge disclosure in the UI
     */
    getAggregatedEdges(request: AggregatedEdgeRequest): Promise<AggregatedEdgeResult>

    // ==========================================
    // Node Creation
    // ==========================================

    /**
     * Create a new node with optional automatic containment edge
     * Validates against ontology rules before creation
     */
    createNode(request: CreateNodeRequest): Promise<CreateNodeResult>
}

// ============================================
// Provider Context Value
// ============================================

export interface GraphProviderContextValue {
    provider: GraphDataProvider | null
    isLoading: boolean
    error: Error | null
}

// ============================================
// Layer Configuration
// ============================================

export interface LayerAssignmentRule {
    /** Rule identifier */
    id: string

    /** Layer this rule assigns to */
    layerId: string

    /** Match by entity types */
    entityTypes?: EntityType[]

    /** Match by tags (any match) */
    tags?: string[]

    /** Match by URN pattern (glob or regex) */
    urnPattern?: string

    /** Match by property value */
    propertyMatch?: {
        field: string
        operator?: 'equals' | 'contains' | 'startsWith' | 'exists'
        value?: unknown
    }

    /** Match by compound conditions (Phase 3) */
    conditions?: RuleCondition[]

    /** Priority for conflict resolution (higher wins) */
    priority: number
}

/**
 * Resolve layer assignment for a node based on rules
 */
/**
 * Check if a node matches a specific rule's criteria
 */
/**
 * Helper to evaluate a single condition against a node
 */
function evaluateCondition(node: GraphNode, field: string, operator: string, targetValue: any): boolean {
    // Resolve value from node properties OR top-level fields
    let actualValue = node.properties[field]
    if (actualValue === undefined) {
        // Fallback to top-level fields
        if (field === 'name') actualValue = node.displayName
        else if (field === 'type') actualValue = node.entityType
        else if (field === 'urn') actualValue = node.urn
    }

    switch (operator) {
        case 'exists':
            return actualValue !== undefined && actualValue !== null
        case 'contains':
            if (typeof actualValue === 'string' && typeof targetValue === 'string') {
                return actualValue.toLowerCase().includes(targetValue.toLowerCase())
            } else if (Array.isArray(actualValue)) {
                return actualValue.includes(targetValue)
            }
            return false
        case 'startsWith':
            if (typeof actualValue === 'string' && typeof targetValue === 'string') {
                return actualValue.toLowerCase().startsWith(targetValue.toLowerCase())
            }
            return false
        case 'endsWith':
            if (typeof actualValue === 'string' && typeof targetValue === 'string') {
                return actualValue.toLowerCase().endsWith(targetValue.toLowerCase())
            }
            return false
        case 'notEquals':
            return actualValue !== targetValue
        case 'equals':
        default:
            return actualValue === targetValue
    }
}

export function matchesRule(
    node: GraphNode,
    rule: LayerAssignmentRule | LayerAssignmentRuleConfig
): boolean {
    // 1. Check entity type match
    if (rule.entityTypes && rule.entityTypes.length > 0) {
        if (!rule.entityTypes.includes(node.entityType)) {
            return false
        }
    }

    // 2. Check tag match
    if (rule.tags && rule.tags.length > 0) {
        if (!node.tags || !node.tags.some(t => rule.tags!.includes(t))) {
            return false
        }
    }

    // 3. Check URN pattern match
    if (rule.urnPattern) {
        const regex = new RegExp(rule.urnPattern.replace(/\*/g, '.*'))
        if (!regex.test(node.urn)) {
            return false
        }
    }

    // 4. Check Compound Conditions (Phase 3)
    if (rule.conditions && rule.conditions.length > 0) {
        // All conditions must match (AND)
        for (const condition of rule.conditions) {
            if (!evaluateCondition(node, condition.field, condition.operator, condition.value)) {
                return false
            }
        }
        return true
    }

    // 5. Fallback Check Single Property Match (Legacy)
    if (rule.propertyMatch) {
        const { field, operator = 'equals', value } = rule.propertyMatch
        if (!evaluateCondition(node, field, operator, value)) {
            return false
        }
    }

    return true
}

/**
 * Resolve layer assignment for a node based on rules
 */
export function resolveLayerAssignment(
    node: GraphNode,
    rules: LayerAssignmentRule[]
): string | undefined {
    // Sort by priority (highest first)
    const sortedRules = [...rules].sort((a, b) => b.priority - a.priority)

    for (const rule of sortedRules) {
        if (matchesRule(node, rule)) {
            return rule.layerId
        }
    }

    return undefined
}

/**
 * Recursively find the best logical node assignment for a physical node
 * 
 * Strategy:
 * 1. Depth-First Search: Check children first (specificity).
 * 2. Check rules on the current logical node.
 * 3. Priority: Rules with higher priority win within the same node context?
 *    Actually, users might define rules on "Payment Platform".
 */
export function resolveLogicalAssignment(
    node: GraphNode,
    logicalNodes: LogicalNodeConfig[]
): string | undefined {
    for (const logicalNode of logicalNodes) {
        // 1. Check children first (Depth-First)
        if (logicalNode.children && logicalNode.children.length > 0) {
            const childMatch = resolveLogicalAssignment(node, logicalNode.children)
            if (childMatch) return childMatch
        }

        // 2. Check rules on this node
        if (logicalNode.rules && logicalNode.rules.length > 0) {
            // Check if any rule matches
            // We take the highest priority match if multiple exist? 
            // Or just any match? Assuming logical nodes are distinct enough.
            // Let's sort rules by priority just in case.
            const sortedRules = [...logicalNode.rules].sort((a, b) => b.priority - a.priority)

            for (const rule of sortedRules) {
                if (matchesRule(node, rule)) {
                    return logicalNode.id
                }
            }
        }
    }
    return undefined
}

export interface EntityAssignment {
    entityId: string;
    layerId: string;
    logicalNodeId?: string; // If assigned to a specific logical node within the layer
    ruleId?: string;        // Which rule caused this assignment
    isInherited: boolean;   // True if assigned via parent
    inheritedFromId?: string; // ID of the parent entity providing the assignment
    confidence: number;     // 1.0 for manual/explicit, <1.0 for inference
}

export interface LayerAssignmentResult {
    assignments: Map<string, EntityAssignment>;
    parentMap: Map<string, string>;
    edges: GraphEdge[];
    unassignedEntityIds: string[];
    stats: {
        totalNodes: number;
        assignedNodes: number;
        computeTimeMs: number;
    };
}

export interface LayerAssignmentRequest {
    scopeFilter?: ScopeFilterConfig;
    layers: {
        id: string; // Matches ViewLayerConfig.id
        name: string;
        color: string;
        order: number;
        sequence: number;
        entityTypes: string[];
        rules: LayerAssignmentRuleConfig[];
        logicalNodes?: LogicalNodeConfig[];
        entityAssignments?: EntityAssignmentConfig[];
    }[];
    includeEdges: boolean;
}
