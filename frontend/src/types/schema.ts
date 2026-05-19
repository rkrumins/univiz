/**
 * NexusLineage Schema System
 * 
 * User-defined entity types, hierarchies, and view configurations.
 * Everything is configurable - no hardcoded entity types.
 */

// ============================================
// ENTITY SCHEMA DEFINITIONS
// ============================================

/**
 * Defines a user-configurable entity type
 * Examples: "Domain", "Database", "Schema", "Table", "Column", "Pipeline", "Dashboard"
 */
export interface EntityTypeSchema {
  id: string;                          // Unique identifier (e.g., "domain", "table")
  name: string;                        // Display name (e.g., "Domain", "Table")
  pluralName: string;                  // Plural form (e.g., "Domains", "Tables")
  description?: string;

  // Visual Configuration
  visual: EntityVisualConfig;

  // Field Definitions - what properties this entity has
  fields: EntityFieldDefinition[];

  // Hierarchy Configuration
  hierarchy: EntityHierarchyConfig;

  // Behavior Configuration
  behavior: EntityBehaviorConfig;
}

export interface EntityVisualConfig {
  icon: string;                        // Lucide icon name or custom SVG
  color: string;                       // Primary color (hex or CSS variable)
  colorSecondary?: string;             // Secondary/accent color
  shape: 'rectangle' | 'rounded' | 'pill' | 'diamond' | 'hexagon' | 'circle';
  size: 'xs' | 'sm' | 'md' | 'lg' | 'xl';
  borderStyle: 'solid' | 'dashed' | 'dotted' | 'none';
  showInMinimap: boolean;
}

export interface EntityFieldDefinition {
  id: string;                          // Field identifier
  name: string;                        // Display name
  type: FieldType;
  required: boolean;
  showInNode: boolean;                 // Display in node card
  showInPanel: boolean;                // Display in detail panel
  showInTooltip: boolean;              // Display in hover tooltip
  displayOrder: number;                // Order of display
  format?: FieldFormat;                // How to format the value
}

export type FieldType =
  | 'string'
  | 'number'
  | 'boolean'
  | 'date'
  | 'datetime'
  | 'url'
  | 'email'
  | 'urn'                              // Technical identifier
  | 'tags'                             // Array of strings
  | 'badge'                            // Single highlighted value
  | 'progress'                         // 0-100 percentage
  | 'status'                           // Enum with colors
  | 'user'                             // User reference
  | 'entity_ref'                       // Reference to another entity
  | 'json'                             // Arbitrary JSON
  | 'markdown';                        // Rich text

export interface FieldFormat {
  prefix?: string;
  suffix?: string;
  dateFormat?: string;
  numberFormat?: 'decimal' | 'percentage' | 'compact' | 'currency';
  truncateAt?: number;
  statusColors?: Record<string, string>;  // For status fields
}

export interface EntityHierarchyConfig {
  level: number;                       // 0 = root, higher = deeper
  canContain: string[];                // Entity type IDs this can contain
  canBeContainedBy: string[];          // Entity type IDs that can contain this
  defaultExpanded: boolean;            // Show children by default
  rollUpFields: RollUpConfig[];        // Fields to aggregate from children
}

export interface RollUpConfig {
  sourceField: string;                 // Field in children to aggregate
  targetField: string;                 // Field to store in parent
  aggregation: 'count' | 'sum' | 'avg' | 'min' | 'max' | 'list' | 'distinct';
  label?: string;                      // Display label (e.g., "3 Tables")
}

export interface EntityBehaviorConfig {
  selectable: boolean;
  draggable: boolean;
  expandable: boolean;                 // Can show/hide children
  traceable: boolean;                  // Can start lineage trace from this
  clickAction: 'select' | 'expand' | 'navigate' | 'panel';
  doubleClickAction: 'expand' | 'navigate' | 'trace' | 'edit' | 'panel';
}

// ============================================
// RELATIONSHIP SCHEMA
// ============================================

export interface RelationshipTypeSchema {
  id: string;
  name: string;
  description?: string;

  // Source and Target constraints
  sourceTypes: string[];               // Entity types that can be source
  targetTypes: string[];               // Entity types that can be target

  // Visual Configuration
  visual: RelationshipVisualConfig;

  // Behavior
  bidirectional: boolean;
  showLabel: boolean;
  labelField?: string;                 // Field to use as edge label

  // Ontology classification — populated from resolved backend ontology.
  // Optional so existing hand-authored schemas don't break; defaults to false/'association'.
  isContainment?: boolean;
  isLineage?: boolean;
  category?: 'structural' | 'flow' | 'metadata' | 'association';
}

export interface RelationshipVisualConfig {
  strokeColor: string;
  strokeWidth: number;
  strokeStyle: 'solid' | 'dashed' | 'dotted';
  animated: boolean;
  animationSpeed: 'slow' | 'normal' | 'fast';
  arrowType: 'arrow' | 'arrowclosed' | 'diamond' | 'circle' | 'none';
  curveType: 'bezier' | 'step' | 'straight' | 'smoothstep';
}

// ============================================
// VIEW CONFIGURATION
// ============================================

/**
 * A View is a complete configuration of how to display entities
 * Users can create multiple views for different use cases
 */
export interface ViewConfiguration {
  id: string;
  name: string;
  description?: string;
  icon?: string;

  /**
   * Scope key for per-datasource isolation.
   * Format: "${workspaceId}/${dataSourceId}"
   * If undefined/null, the view is global and visible across all scopes (legacy behaviour).
   */
  scopeKey?: string | null;

  /** Workspace this view belongs to (populated from API, absent for locally-created views). */
  workspaceId?: string;
  /** Datasource this view is scoped to. NULL/undefined = workspace-level (visible for all datasources). */
  dataSourceId?: string | null;
  /** Display name of the workspace (enriched from API). */
  workspaceName?: string;
  /** Whether the current user has bookmarked/favourited this view. */
  isFavourited?: boolean;

  // What to show
  content: ViewContentConfig;

  // How to show it
  layout: ViewLayoutConfig;

  // Filtering
  filters: ViewFilterConfig;

  // Visual overrides per entity type
  entityOverrides: Record<string, Partial<EntityVisualConfig>>;

  // Grouping configuration
  grouping?: ViewGroupingConfig;

  // Permissions
  isDefault: boolean;
  isPublic: boolean;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
}

export interface ViewContentConfig {
  // Which entity types are visible in this view
  visibleEntityTypes: string[];

  // Which relationship types are visible
  visibleRelationshipTypes: string[];

  // Default hierarchy depth to show
  defaultDepth: number;

  // Max hierarchy depth allowed
  maxDepth: number;

  // Root entity types (entry points for navigation)
  rootEntityTypes: string[];
}

export interface ViewLayoutConfig {
  type: 'graph' | 'tree' | 'hierarchy' | 'reference' | 'layered-lineage' | 'list' | 'grid' | 'timeline';

  // Graph-specific
  graphLayout?: {
    algorithm: 'dagre' | 'elk' | 'force' | 'radial' | 'manual';
    direction: 'LR' | 'RL' | 'TB' | 'BT';
    nodeSpacing: number;
    levelSpacing: number;
  };

  // Tree-specific
  treeLayout?: {
    orientation: 'horizontal' | 'vertical';
    compactMode: boolean;
  };

  // Reference Model specific (horizontal layer columns)
  referenceLayout?: {
    layers: ViewLayerConfig[];
  };

  // LOD (Level of Detail) configuration
  lod: LODConfig;

  // Projection/Aggregation configuration
  projection?: ViewProjectionConfig;
}

/**
 * Layer configuration for Reference Model view
 */
export interface ViewLayerConfig {
  id: string;
  name: string;
  description?: string;
  icon?: string;
  color?: string;
  entityTypes: string[];
  order: number;
  sequence?: number; // Visual order (left-to-right)

  // Logical Hierarchy (New)
  logicalNodes?: LogicalNodeConfig[];
  showUnassigned?: boolean; // Whether to show unmapped physical entities

  // Advanced assignment rules (overrides entityTypes)
  rules?: LayerAssignmentRuleConfig[];

  // Instance-level assignments (highest priority - direct entity mapping)
  entityAssignments?: EntityAssignmentConfig[];

  // Scope configuration - which edge types define containment hierarchy
  scopeEdges?: ScopeEdgeConfig;
}

/**
 * Direct entity-to-layer assignment (overrides rule-based matching)
 * Enables assigning specific entities (e.g., "Finance Domain") to layers
 * regardless of their entity type.
 */
export interface EntityAssignmentConfig {
  /** URN or ID of the specific entity being assigned */
  entityId: string;

  /** Target layer ID */
  layerId: string;

  /** Optional logical node within the layer */
  logicalNodeId?: string;

  /**
   * If true, all descendants (children, grandchildren, etc.) inherit this assignment.
   * If false, only this specific entity is assigned; children use rules/defaults.
   */
  inheritsChildren: boolean;

  /**
   * Priority for conflict resolution (higher wins).
   * - 1000+ for manual user assignments
   * - 100-999 for rule-based assignments
   * - <100 for type-based defaults
   */
  priority: number;

  /** Optional: who/what assigned this (for audit trail) */
  assignedBy?: 'user' | 'rule' | 'inference';

  /** Timestamp of assignment */
  assignedAt?: string;
}

/**
 * Edge-based scope filtering for layer assignment.
 * Controls which relationship types define the containment hierarchy.
 */
export interface ScopeEdgeConfig {
  /**
   * Specific edge types to include in scope.
   * Examples: ['CONTAINS', 'BELONGS_TO', 'HAS_CHILD']
   */
  edgeTypes: string[];

  /**
   * If true, all edge types are included regardless of edgeTypes array.
   * Useful for "include everything except..." logic when combined with excludeEdgeTypes.
   */
  includeAll: boolean;

  /** Edge types to explicitly exclude (only applies when includeAll is true) */
  excludeEdgeTypes?: string[];
}

/**
 * Result of checking an assignment for conflicts
 */
export interface AssignmentConflict {
  /** The entity being assigned */
  entityId: string;

  /** Parent or child entity causing the conflict */
  conflictingEntityId: string;

  /** Type of conflict */
  type: 'parent_assigned' | 'child_assigned' | 'circular' | 'containment_locked';

  /** Human-readable message */
  message: string;

  /** The conflicting assignment's layer */
  conflictingLayerId: string;
}



export interface LogicalNodeConfig {
  id: string;
  name: string;
  description?: string;
  type: 'container' | 'system' | 'group';
  children?: LogicalNodeConfig[];
  rules?: LayerAssignmentRuleConfig[];

  // Visual state
  collapsed?: boolean;
}

export interface LayerAssignmentRuleConfig {
  id: string;
  name?: string;
  description?: string;

  // Match criteria (OR logic between different fields, AND logic within same field if array)
  entityTypes?: string[];
  tags?: string[];
  urnPattern?: string;
  /**
   * Restricts this rule to descendants of `scopeRootUrn` via the containment
   * parent chain computed by the backend AssignmentEngine. Lets a rule target
   * "all <type> under entity P" without materializing per-entity
   * EntityAssignmentConfig rows.
   */
  scopeRootUrn?: string;
  propertyMatch?: {
    field: string;
    operator: 'equals' | 'contains' | 'startsWith' | 'exists';
    value: unknown;
  };

  priority: number; // Higher wins

  /**
   * If true, when this rule matches an entity, all of its descendants
   * (children, grandchildren, etc.) automatically inherit this layer assignment.
   * Default: true (existing behavior)
   */
  inheritsFromParent?: boolean;

  // Compound Rules (Phase 3)
  // If present, ALL conditions must match (AND logic)
  // Replaces the single propertyMatch if both exist (or works in conjunction)
  conditions?: RuleCondition[];
}

export interface RuleCondition {
  field: string;
  operator: 'equals' | 'contains' | 'startsWith' | 'exists' | 'endsWith' | 'notEquals';
  value: unknown;
}

/**
 * How to project/aggregate the underlying data for this view
 */
export interface ViewProjectionConfig {
  // Target entity type ID for lineage aggregation (e.g. "dataset", "term").
  // null = no aggregation (show finest-grained lineage as-is).
  targetGranularityType: string | null;

  // Aggregate child lineage to parent level
  aggregateLineage: boolean;

  // Collapse children, show roll-up counts
  collapseChildren: boolean;

  // Entity types that act as visual containers
  containerTypes: string[];

  // Edge types that constitute a containment relationship
  containmentEdgeTypes?: string[];
}

// ============================================
// LINEAGE EXPLORATION CONFIGURATION
// ============================================

/**
 * Exploration Mode determines how lineage is initially loaded and expanded
 */
export type LineageExplorationMode =
  | 'overview'   // Top-down: Start aggregated, expand layer by layer
  | 'focused'    // Bottom-up: Start from a target, expand N levels
  | 'full'       // Show everything at once (for small graphs)
  | 'search';    // Search-based: Show results and their context

/**
 * Granularity determines what level of detail is shown
 */
export type LineageGranularity =
  | 'column'     // Show column-level lineage
  | 'table'      // Aggregate to table level
  | 'schema'     // Aggregate to schema level  
  | 'system'     // Aggregate to system level
  | 'domain';    // Aggregate to domain level

/**
 * Complete configuration for lineage exploration
 */
export interface LineageExplorationConfig {
  // Exploration Mode
  mode: LineageExplorationMode;

  // Current granularity level
  granularity: LineageGranularity;

  // Focus configuration (for 'focused' mode)
  focus?: {
    entityId: string;            // The entity to focus on
    includeAncestors: boolean;   // Show parent entities (table → schema → domain)
    includeDescendants: boolean; // Show child entities (table → columns)
  };

  // Trace configuration
  trace: {
    upstreamDepth: number;       // How many levels upstream (0 = none)
    downstreamDepth: number;     // How many levels downstream (0 = none)
    includeChildLineage: boolean; // When at table level, include column lineage in trace
    maxNodes: number;            // Max nodes to load (performance limit)
  };

  // Aggregation behavior
  aggregation: {
    // When true, if any child has lineage, parent shows lineage
    inheritFromChildren: boolean;

    // Show aggregated edges with source count badge
    showAggregatedEdges: boolean;

    // Min confidence to show aggregated edge (0-1)
    minConfidence: number;
  };

  // Expansion state
  expansion: {
    // Entity IDs that have been manually expanded
    expandedIds: Set<string>;

    // Whether to auto-expand on focus
    autoExpandOnFocus: boolean;

    // Default expansion depth for overview mode
    defaultExpandDepth: number;
  };

  // Visual toggles
  display: {
    showGhostNodes: boolean;     // Show collapsed/offscreen indicators
    showConfidence: boolean;     // Show confidence scores on edges
    showCounts: boolean;         // Show child counts on collapsed nodes
    highlightPath: boolean;      // Highlight lineage path on selection
  };

  // Hierarchy Configuration
  containmentEdgeTypes?: string[];
}

/**
 * Default exploration configurations for common use cases
 */
export const DEFAULT_EXPLORATION_CONFIGS: Record<string, Partial<LineageExplorationConfig>> = {
  // Overview: Start high-level, expand on demand
  overview: {
    mode: 'overview',
    granularity: 'table',
    trace: {
      upstreamDepth: 2,
      downstreamDepth: 2,
      includeChildLineage: true,
      maxNodes: 100,
    },
    aggregation: {
      inheritFromChildren: true,
      showAggregatedEdges: true,
      minConfidence: 0.3,
    },
    expansion: {
      expandedIds: new Set(),
      autoExpandOnFocus: false,
      defaultExpandDepth: 1,
    },
    display: {
      showGhostNodes: true,
      showConfidence: false,
      showCounts: true,
      highlightPath: true,
    },
  },

  // Technical: Deep dive from a specific entity
  technical: {
    mode: 'focused',
    granularity: 'column',
    trace: {
      upstreamDepth: 5,
      downstreamDepth: 5,
      includeChildLineage: false,
      maxNodes: 200,
    },
    aggregation: {
      inheritFromChildren: false,
      showAggregatedEdges: false,
      minConfidence: 0,
    },
    expansion: {
      expandedIds: new Set(),
      autoExpandOnFocus: true,
      defaultExpandDepth: 3,
    },
    display: {
      showGhostNodes: true,
      showConfidence: true,
      showCounts: false,
      highlightPath: true,
    },
  },

  // Impact Analysis: Table-level with child inheritance
  impact: {
    mode: 'focused',
    granularity: 'table',
    trace: {
      upstreamDepth: 10,
      downstreamDepth: 10,
      includeChildLineage: true, // Table shows impact from all columns
      maxNodes: 150,
    },
    aggregation: {
      inheritFromChildren: true,
      showAggregatedEdges: true,
      minConfidence: 0.5,
    },
    expansion: {
      expandedIds: new Set(),
      autoExpandOnFocus: true,
      defaultExpandDepth: 2,
    },
    display: {
      showGhostNodes: true,
      showConfidence: true,
      showCounts: true,
      highlightPath: true,
    },
  },
};

export interface LODConfig {
  enabled: boolean;
  levels: LODLevel[];
}

export interface LODLevel {
  name: string;
  zoomRange: [number, number];         // [minZoom, maxZoom]
  visibleEntityTypes: string[];
  showLabels: boolean;
  showIcons: boolean;
  showBadges: boolean;
  aggregateChildren: boolean;          // Show child count instead of nodes
}

export interface ViewFilterConfig {
  // Persistent filters for this view
  entityTypeFilters: string[];
  fieldFilters: FieldFilter[];

  // Search configuration
  searchableFields: string[];

  // Quick filter buttons
  quickFilters: QuickFilter[];
}

export interface FieldFilter {
  field: string;
  operator: 'equals' | 'contains' | 'startsWith' | 'endsWith' | 'gt' | 'lt' | 'in' | 'notIn';
  value: unknown;
}

export interface QuickFilter {
  id: string;
  label: string;
  icon?: string;
  filter: FieldFilter[];
}

export interface ViewGroupingConfig {
  enabled: boolean;
  groupByField: string;                // Field to group by
  groupVisual: {
    showHeader: boolean;
    collapsible: boolean;
    color?: string;
  };
}

// ============================================
// WORKSPACE SCHEMA
// ============================================

/**
 * Frontend store shape: ontology-derived schema, views, and active display scope.
 */
export interface WorkspaceSchema {
  id: string;
  name: string;
  version: string;

  // Entity type definitions
  entityTypes: EntityTypeSchema[];

  // Relationship type definitions
  relationshipTypes: RelationshipTypeSchema[];

  // View configurations
  views: ViewConfiguration[];

  // Default view ID
  defaultViewId: string;

  // Global visual settings
  globalVisuals: GlobalVisualConfig;

  /**
   * Default containment edge types for hierarchy traversal.
   * Used when building parent-child relationships for tree displays.
   * Examples: ['CONTAINS', 'HAS_SCHEMA', 'HAS_COLUMN', 'BELONGS_TO']
   * Can be overridden per-view via ViewLayerConfig.scopeEdges
   */
  containmentEdgeTypes?: string[];

  /**
   * Default lineage/flow edge types.
   * Derived from relationship definitions with isLineage = true.
   * Examples: ['FLOWS_TO', 'CONSUMES', 'PRODUCES', 'DERIVED_FROM']
   */
  lineageEdgeTypes?: string[];

  /**
   * Root entity types — entry points for graph traversal (e.g. 'domain').
   * Derived from ontology resolution.
   */
  rootEntityTypes?: string[];
}

export interface GlobalVisualConfig {
  theme: 'light' | 'dark' | 'system';
  accentColor: string;
  fontFamily: string;
  borderRadius: 'none' | 'sm' | 'md' | 'lg' | 'full';
  showConfidenceScores: boolean;
  animationsEnabled: boolean;
}

// ============================================
// ENTITY INSTANCE (Runtime Data)
// ============================================

/**
 * An actual entity instance in the graph
 */
export interface EntityInstance {
  id: string;
  typeId: string;                      // References EntityTypeSchema.id

  // Core data
  data: Record<string, unknown>;       // Field values

  // Hierarchy
  parentId?: string;
  childIds: string[];

  // Position (for graph layout)
  position?: { x: number; y: number };

  // Computed/cached values
  _computed?: {
    rollUps: Record<string, unknown>;
    depth: number;
    path: string[];                    // Ancestor IDs
  };
}

export interface RelationshipInstance {
  id: string;
  typeId: string;                      // References RelationshipTypeSchema.id
  sourceId: string;
  targetId: string;
  data?: Record<string, unknown>;
}

/**
 * Configuration for scope-based filtering
 */
export interface ScopeFilterConfig {
  id: string;
  name: string;
  description?: string;
  rules: ScopeFilterRule[];
}

export interface ScopeFilterRule {
  field: string;
  operator: 'equals' | 'contains' | 'startsWith' | 'in';
  value: unknown;
}

