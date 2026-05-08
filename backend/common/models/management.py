"""
Pydantic models for the management database layer.
Covers: graph connections, ontology configs, assignment rule sets, saved views.
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum


class ProviderType(str, Enum):
    FALKORDB = "falkordb"
    NEO4J = "neo4j"
    DATAHUB = "datahub"


# ============================================
# Connection Models
# ============================================

class ConnectionCredentials(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None

    class Config:
        populate_by_name = True


class ConnectionCreateRequest(BaseModel):
    name: str
    provider_type: ProviderType
    host: Optional[str] = None
    port: Optional[int] = None
    graph_name: Optional[str] = None
    credentials: Optional[ConnectionCredentials] = None
    tls_enabled: bool = False
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")

    class Config:
        populate_by_name = True


class ConnectionUpdateRequest(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    graph_name: Optional[str] = Field(None, alias="graphName")
    credentials: Optional[ConnectionCredentials] = None
    tls_enabled: Optional[bool] = Field(None, alias="tlsEnabled")
    is_active: Optional[bool] = Field(None, alias="isActive")
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")

    class Config:
        populate_by_name = True


class ConnectionResponse(BaseModel):
    id: str
    name: str
    provider_type: ProviderType = Field(alias="providerType")
    host: Optional[str] = None
    port: Optional[int] = None
    graph_name: Optional[str] = Field(None, alias="graphName")
    tls_enabled: bool = Field(alias="tlsEnabled")
    is_primary: bool = Field(alias="isPrimary")
    is_active: bool = Field(alias="isActive")
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    # credentials are NEVER returned

    class Config:
        populate_by_name = True


class ConnectionTestResult(BaseModel):
    success: bool
    latency_ms: Optional[float] = Field(None, alias="latencyMs")
    error: Optional[str] = None
    provider_version: Optional[str] = Field(None, alias="providerVersion")

    class Config:
        populate_by_name = True


class GraphListResponse(BaseModel):
    graphs: List[str]
    connection_id: str = Field(alias="connectionId")

    class Config:
        populate_by_name = True


# ============================================
# Assignment Rule Set Models
# ============================================

class RuleSetCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    layers_config: List[Dict[str, Any]] = Field(alias="layersConfig")
    is_default: bool = Field(False, alias="isDefault")

    class Config:
        populate_by_name = True


class RuleSetResponse(BaseModel):
    id: str
    connection_id: str = Field(alias="connectionId")
    name: str
    description: Optional[str] = None
    is_default: bool = Field(alias="isDefault")
    layers_config: List[Dict[str, Any]] = Field(alias="layersConfig")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


# ============================================
# Management DB Config Model
# ============================================

class StorageBackend(str, Enum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    FALKORDB = "falkordb"


class ManagementDbConfig(BaseModel):
    storage_backend: StorageBackend = Field(StorageBackend.SQLITE, alias="storageBackend")
    falkordb_conn_id: Optional[str] = Field(None, alias="falkordbConnId")
    falkordb_graph_name: Optional[str] = Field(None, alias="falkordbGraphName")
    postgres_url: Optional[str] = Field(None, alias="postgresUrl")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    class Config:
        populate_by_name = True


# ============================================
# Provider Models (workspace-centric)
# ============================================

class ProviderCreateRequest(BaseModel):
    name: str
    provider_type: ProviderType = Field(alias="providerType")
    host: Optional[str] = None
    port: Optional[int] = None
    credentials: Optional[ConnectionCredentials] = None
    tls_enabled: bool = Field(False, alias="tlsEnabled")
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")

    class Config:
        populate_by_name = True


class ProviderUpdateRequest(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    credentials: Optional[ConnectionCredentials] = None
    tls_enabled: Optional[bool] = Field(None, alias="tlsEnabled")
    is_active: Optional[bool] = Field(None, alias="isActive")
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")

    class Config:
        populate_by_name = True


class ProviderResponse(BaseModel):
    id: str
    name: str
    provider_type: ProviderType = Field(alias="providerType")
    host: Optional[str] = None
    port: Optional[int] = None
    tls_enabled: bool = Field(alias="tlsEnabled")
    is_active: bool = Field(alias="isActive")
    extra_config: Optional[Dict[str, Any]] = Field(None, alias="extraConfig")
    permitted_workspaces: List[str] = Field(default_factory=lambda: ["*"], alias="permittedWorkspaces")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    # credentials are NEVER returned

    class Config:
        populate_by_name = True


# ============================================
# Ontology Definition Models (standalone, versioned)
# ============================================

class OntologyCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    version: int = 1
    scope: str = "universal"  # "universal" | "workspace"
    # Schema evolution policy: what happens when this ontology is updated and published.
    # reject   — block publish if it breaks existing data (default, safest).
    # deprecate — mark removed/renamed types deprecated; still served.
    # migrate  — apply a migration manifest to remap types automatically.
    evolution_policy: str = Field("reject", alias="evolutionPolicy")
    containment_edge_types: List[str] = Field(default_factory=list, alias="containmentEdgeTypes")
    lineage_edge_types: List[str] = Field(default_factory=list, alias="lineageEdgeTypes")
    edge_type_metadata: Dict[str, Any] = Field(default_factory=dict, alias="edgeTypeMetadata")
    entity_type_hierarchy: Dict[str, Any] = Field(default_factory=dict, alias="entityTypeHierarchy")
    root_entity_types: List[str] = Field(default_factory=list, alias="rootEntityTypes")
    entity_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="entityTypeDefinitions")
    relationship_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="relationshipTypeDefinitions")

    class Config:
        populate_by_name = True


class OntologyUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    evolution_policy: Optional[str] = Field(None, alias="evolutionPolicy")
    containment_edge_types: Optional[List[str]] = Field(None, alias="containmentEdgeTypes")
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    edge_type_metadata: Optional[Dict[str, Any]] = Field(None, alias="edgeTypeMetadata")
    entity_type_hierarchy: Optional[Dict[str, Any]] = Field(None, alias="entityTypeHierarchy")
    root_entity_types: Optional[List[str]] = Field(None, alias="rootEntityTypes")
    entity_type_definitions: Optional[Dict[str, Any]] = Field(None, alias="entityTypeDefinitions")
    relationship_type_definitions: Optional[Dict[str, Any]] = Field(None, alias="relationshipTypeDefinitions")

    class Config:
        populate_by_name = True


class OntologyImportRequest(BaseModel):
    """
    Validated import payload — mirrors the export JSON format exactly.
    All semantic fields are required so we can detect what changed.
    Metadata fields (id, timestamps, status flags) are accepted but ignored on import.
    """
    # Metadata (accepted from export JSON, ignored during import)
    id: Optional[str] = None
    version: Optional[int] = None
    scope: Optional[str] = None
    is_published: Optional[bool] = Field(None, alias="isPublished")
    is_system: Optional[bool] = Field(None, alias="isSystem")
    created_at: Optional[str] = Field(None, alias="createdAt")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    # Required semantic content
    name: str
    description: Optional[str] = None
    evolution_policy: str = Field("reject", alias="evolutionPolicy")
    entity_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="entityTypeDefinitions")
    relationship_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="relationshipTypeDefinitions")
    containment_edge_types: List[str] = Field(default_factory=list, alias="containmentEdgeTypes")
    lineage_edge_types: List[str] = Field(default_factory=list, alias="lineageEdgeTypes")
    edge_type_metadata: Dict[str, Any] = Field(default_factory=dict, alias="edgeTypeMetadata")
    entity_type_hierarchy: Dict[str, Any] = Field(default_factory=dict, alias="entityTypeHierarchy")
    root_entity_types: List[str] = Field(default_factory=list, alias="rootEntityTypes")

    class Config:
        populate_by_name = True


class OntologyImportResponse(BaseModel):
    """Result of an import operation."""
    ontology: "OntologyDefinitionResponse"
    status: str  # "created" | "updated" | "new_version" | "no_changes"
    summary: str
    changes: Optional[Dict[str, Any]] = None  # type diff if applicable

    class Config:
        populate_by_name = True


class OntologyDefinitionResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    version: int
    evolution_policy: str = Field("reject", alias="evolutionPolicy")
    containment_edge_types: List[str] = Field(alias="containmentEdgeTypes")
    lineage_edge_types: List[str] = Field(alias="lineageEdgeTypes")
    edge_type_metadata: Dict[str, Any] = Field(alias="edgeTypeMetadata")
    entity_type_hierarchy: Dict[str, Any] = Field(alias="entityTypeHierarchy")
    root_entity_types: List[str] = Field(alias="rootEntityTypes")
    entity_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="entityTypeDefinitions")
    relationship_type_definitions: Dict[str, Any] = Field(default_factory=dict, alias="relationshipTypeDefinitions")
    is_published: bool = Field(alias="isPublished")
    is_system: bool = Field(False, alias="isSystem")
    scope: str = "universal"
    schema_id: str = Field("", alias="schemaId")
    revision: int = Field(0)
    created_by: Optional[str] = Field(None, alias="createdBy")
    updated_by: Optional[str] = Field(None, alias="updatedBy")
    published_by: Optional[str] = Field(None, alias="publishedBy")
    published_at: Optional[str] = Field(None, alias="publishedAt")
    deleted_by: Optional[str] = Field(None, alias="deletedBy")
    deleted_at: Optional[str] = Field(None, alias="deletedAt")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


class OntologyAuditEntry(BaseModel):
    id: str
    ontology_id: str = Field(alias="ontologyId")
    schema_id: str = Field(alias="schemaId")
    action: str
    actor: Optional[str] = None
    version: Optional[int] = None
    summary: Optional[str] = None
    changes: Optional[Dict[str, Any]] = None
    created_at: str = Field(alias="createdAt")

    class Config:
        populate_by_name = True


class OntologyValidationIssue(BaseModel):
    severity: str  # "error" | "warning"
    code: str
    message: str
    affected: Optional[str] = None


class OntologyValidationResponse(BaseModel):
    is_valid: bool = Field(alias="isValid")
    issues: List[OntologyValidationIssue] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class OntologyCoverageResponse(BaseModel):
    coverage_percent: float = Field(alias="coveragePercent")
    covered_entity_types: List[str] = Field(default_factory=list, alias="coveredEntityTypes")
    uncovered_entity_types: List[str] = Field(default_factory=list, alias="uncoveredEntityTypes")
    extra_entity_types: List[str] = Field(default_factory=list, alias="extraEntityTypes")
    covered_relationship_types: List[str] = Field(default_factory=list, alias="coveredRelationshipTypes")
    uncovered_relationship_types: List[str] = Field(default_factory=list, alias="uncoveredRelationshipTypes")

    class Config:
        populate_by_name = True


class OntologyResolutionRelGap(BaseModel):
    """A relationship type missing classification flags."""
    id: str
    name: str
    is_containment: Optional[bool] = Field(None, alias="isContainment")
    is_lineage: Optional[bool] = Field(None, alias="isLineage")

    class Config:
        populate_by_name = True


class OntologyResolutionHierarchyGap(BaseModel):
    """An entity type with an incomplete hierarchy field. Advisory."""
    entity_type: str = Field(alias="entityType")
    missing_field: str = Field(alias="missingField")

    class Config:
        populate_by_name = True


class OntologyResolutionResponse(BaseModel):
    """Per-data-source ontology resolution gate report.

    Drives the AssetOnboardingWizard SchemaReviewStep and is enforced
    by AggregationService.trigger before any aggregation job is created.
    """
    resolved: bool
    ontology_id: Optional[str] = Field(None, alias="ontologyId")
    ontology_version: Optional[int] = Field(None, alias="ontologyVersion")
    ontology_is_published: bool = Field(False, alias="ontologyIsPublished")
    missing_entity_types: List[str] = Field(default_factory=list, alias="missingEntityTypes")
    missing_edge_types: List[str] = Field(default_factory=list, alias="missingEdgeTypes")
    unclassified_relationships: List[OntologyResolutionRelGap] = Field(
        default_factory=list, alias="unclassifiedRelationships"
    )
    has_lineage: bool = Field(False, alias="hasLineage")
    has_containment: bool = Field(False, alias="hasContainment")
    hierarchy_warnings: List[OntologyResolutionHierarchyGap] = Field(
        default_factory=list, alias="hierarchyWarnings"
    )
    advisory_warnings: List[str] = Field(default_factory=list, alias="advisoryWarnings")
    blocking_reasons: List[str] = Field(default_factory=list, alias="blockingReasons")
    fingerprint: Optional[str] = None

    class Config:
        populate_by_name = True


class OntologyMatchResult(BaseModel):
    ontology_id: str = Field(alias="ontologyId")
    ontology_name: str = Field(alias="ontologyName")
    version: int
    jaccard_score: float = Field(alias="jaccardScore")
    covered_entity_types: List[str] = Field(default_factory=list, alias="coveredEntityTypes")
    uncovered_entity_types: List[str] = Field(default_factory=list, alias="uncoveredEntityTypes")
    covered_relationship_types: List[str] = Field(default_factory=list, alias="coveredRelationshipTypes")
    uncovered_relationship_types: List[str] = Field(default_factory=list, alias="uncoveredRelationshipTypes")
    total_entity_types: int = Field(0, alias="totalEntityTypes")
    total_relationship_types: int = Field(0, alias="totalRelationshipTypes")

    class Config:
        populate_by_name = True


class OntologySuggestResponse(BaseModel):
    suggested: OntologyCreateRequest
    matching_ontologies: List[OntologyMatchResult] = Field(default_factory=list, alias="matchingOntologies")

    class Config:
        populate_by_name = True


# ============================================
# Data Source Models (workspace data sources)
# ============================================

class DataSourceCreateRequest(BaseModel):
    provider_id: Optional[str] = Field(None, alias="providerId")
    catalog_item_id: Optional[str] = Field(None, alias="catalogItemId")
    graph_name: Optional[str] = Field(None, alias="graphName")
    ontology_id: Optional[str] = Field(None, alias="ontologyId")
    label: Optional[str] = None
    access_level: Optional[str] = Field(None, alias="accessLevel")  # read | write | admin
    extra_config: Optional[dict] = Field(None, alias="extraConfig")  # per-data-source config (schema mapping, etc.)

    class Config:
        populate_by_name = True


class DataSourceUpdateRequest(BaseModel):
    provider_id: Optional[str] = Field(None, alias="providerId")
    graph_name: Optional[str] = Field(None, alias="graphName")
    ontology_id: Optional[str] = Field(None, alias="ontologyId")
    label: Optional[str] = None
    is_active: Optional[bool] = Field(None, alias="isActive")
    projection_mode: Optional[str] = Field(None, alias="projectionMode")  # None | "in_source" | "dedicated"
    dedicated_graph_name: Optional[str] = Field(None, alias="dedicatedGraphName")  # graph name when dedicated
    extra_config: Optional[dict] = Field(None, alias="extraConfig")  # per-data-source config (schema mapping, etc.)

    class Config:
        populate_by_name = True


class DataSourceResponse(BaseModel):
    id: str
    workspace_id: str = Field(alias="workspaceId")
    provider_id: Optional[str] = Field(None, alias="providerId")
    catalog_item_id: Optional[str] = Field(None, alias="catalogItemId")
    graph_name: Optional[str] = Field(None, alias="graphName")
    ontology_id: Optional[str] = Field(None, alias="ontologyId")
    label: Optional[str] = None
    is_primary: bool = Field(alias="isPrimary")
    is_active: bool = Field(alias="isActive")
    projection_mode: Optional[str] = Field(None, alias="projectionMode")
    dedicated_graph_name: Optional[str] = Field(None, alias="dedicatedGraphName")
    access_level: Optional[str] = Field(None, alias="accessLevel")  # read | write | admin
    extra_config: Optional[dict] = Field(None, alias="extraConfig")
    # Aggregation state
    aggregation_status: str = Field("none", alias="aggregationStatus")
    last_aggregated_at: Optional[str] = Field(None, alias="lastAggregatedAt")
    aggregation_edge_count: int = Field(0, alias="aggregationEdgeCount")
    aggregation_schedule: Optional[str] = Field(None, alias="aggregationSchedule")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


# ============================================
# Workspace Models (workspace-centric)
# ============================================

class WorkspaceCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    data_sources: List[DataSourceCreateRequest] = Field(alias="dataSources")

    class Config:
        populate_by_name = True


class WorkspaceUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = Field(None, alias="isActive")

    class Config:
        populate_by_name = True


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    data_sources: List[DataSourceResponse] = Field(default_factory=list, alias="dataSources")
    is_default: bool = Field(alias="isDefault")
    is_active: bool = Field(alias="isActive")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True

    @property
    def primary_data_source(self) -> Optional[DataSourceResponse]:
        """Return the primary data source, or first if none marked primary."""
        return next(
            (ds for ds in self.data_sources if ds.is_primary),
            self.data_sources[0] if self.data_sources else None,
        )

    @property
    def provider_id(self) -> Optional[str]:
        """Convenience: provider_id from primary data source (backward compat)."""
        ds = self.primary_data_source
        return ds.provider_id if ds else None

    @property
    def graph_name(self) -> Optional[str]:
        """Convenience: graph_name from primary data source (backward compat)."""
        ds = self.primary_data_source
        return ds.graph_name if ds else None

    @property
    def ontology_id(self) -> Optional[str]:
        """Convenience: ontology_id from primary data source."""
        ds = self.primary_data_source
        return ds.ontology_id if ds else None


# ============================================
# Context Model Models
# ============================================

class ContextModelCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    is_template: bool = Field(False, alias="isTemplate")
    category: Optional[str] = None
    layers_config: List[Dict[str, Any]] = Field(default_factory=list, alias="layersConfig")
    scope_filter: Optional[Dict[str, Any]] = Field(None, alias="scopeFilter")
    instance_assignments: Dict[str, Any] = Field(default_factory=dict, alias="instanceAssignments")
    scope_edge_config: Optional[Dict[str, Any]] = Field(None, alias="scopeEdgeConfig")

    class Config:
        populate_by_name = True


class ContextModelUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    layers_config: Optional[List[Dict[str, Any]]] = Field(None, alias="layersConfig")
    scope_filter: Optional[Dict[str, Any]] = Field(None, alias="scopeFilter")
    instance_assignments: Optional[Dict[str, Any]] = Field(None, alias="instanceAssignments")
    scope_edge_config: Optional[Dict[str, Any]] = Field(None, alias="scopeEdgeConfig")

    class Config:
        populate_by_name = True


class ContextModelResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    workspace_id: Optional[str] = Field(None, alias="workspaceId")
    data_source_id: Optional[str] = Field(None, alias="dataSourceId")
    is_template: bool = Field(alias="isTemplate")
    category: Optional[str] = None
    layers_config: List[Dict[str, Any]] = Field(default_factory=list, alias="layersConfig")
    scope_filter: Optional[Dict[str, Any]] = Field(None, alias="scopeFilter")
    instance_assignments: Dict[str, Any] = Field(default_factory=dict, alias="instanceAssignments")
    scope_edge_config: Optional[Dict[str, Any]] = Field(None, alias="scopeEdgeConfig")
    is_active: bool = Field(alias="isActive")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


class InstantiateTemplateRequest(BaseModel):
    template_id: str = Field(alias="templateId")
    name: str

    class Config:
        populate_by_name = True


# ============================================
# View Models (visual rendering of context models)
# ============================================

class ViewCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    context_model_id: Optional[str] = Field(None, alias="contextModelId")
    workspace_id: str = Field(alias="workspaceId")
    data_source_id: Optional[str] = Field(None, alias="dataSourceId")
    view_type: str = Field("graph", alias="viewType")
    config: Dict[str, Any] = Field(default_factory=dict)
    visibility: str = "private"
    tags: Optional[List[str]] = None
    is_pinned: bool = Field(False, alias="isPinned")

    class Config:
        populate_by_name = True


class ViewUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    context_model_id: Optional[str] = Field(None, alias="contextModelId")
    view_type: Optional[str] = Field(None, alias="viewType")
    config: Optional[Dict[str, Any]] = None
    visibility: Optional[str] = None
    tags: Optional[List[str]] = None
    is_pinned: Optional[bool] = Field(None, alias="isPinned")

    class Config:
        populate_by_name = True


class ViewResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    context_model_id: Optional[str] = Field(None, alias="contextModelId")
    context_model_name: Optional[str] = Field(None, alias="contextModelName")
    workspace_id: str = Field(alias="workspaceId")
    workspace_name: Optional[str] = Field(None, alias="workspaceName")
    data_source_id: Optional[str] = Field(None, alias="dataSourceId")
    data_source_name: Optional[str] = Field(None, alias="dataSourceName")
    view_type: str = Field(alias="viewType")
    # Layout algorithm (reference | hierarchical | force | …). Projected from
    # config.layoutType so metadata-only consumers (e.g. the ViewWizard's
    # scope resolver) can branch on it without parsing the full config.
    layout_type: Optional[str] = Field(None, alias="layoutType")
    config: Dict[str, Any] = Field(default_factory=dict)
    visibility: str = "private"
    created_by: Optional[str] = Field(None, alias="createdBy")
    # Human-readable display for the creator, resolved server-side so
    # every UI surface can show "Alex Smith" rather than "usr_abc123".
    # Nullable for legacy rows where the user record has since been
    # deleted — callers fall back to ``created_by`` in that case.
    created_by_name: Optional[str] = Field(None, alias="createdByName")
    created_by_email: Optional[str] = Field(None, alias="createdByEmail")
    tags: Optional[List[str]] = None
    is_pinned: bool = Field(False, alias="isPinned")
    favourite_count: int = Field(0, alias="favouriteCount")
    is_favourited: bool = Field(False, alias="isFavourited")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    deleted_at: Optional[str] = Field(None, alias="deletedAt")
    # Ontology digest captured at view save time. When the wizard opens a
    # view for edit, it compares this against the currently-resolved ontology
    # digest; a mismatch surfaces a non-blocking drift banner so the user
    # knows some entity classifications may have changed since creation.
    # NULL on legacy rows → wizard treats as "drift check unavailable".
    ontology_digest: Optional[str] = Field(None, alias="ontologyDigest")

    class Config:
        populate_by_name = True


class ViewListResponse(BaseModel):
    """Paginated view listing envelope.

    Returned by GET /api/v1/views/ so callers get an authoritative
    ``total`` count and a pre-computed ``has_more`` flag instead of
    guessing from ``len(items) >= limit``. ``next_offset`` is the
    offset to pass for the next page, or None when ``has_more`` is
    False.
    """
    items: List[ViewResponse]
    total: int
    has_more: bool = Field(alias="hasMore")
    next_offset: Optional[int] = Field(None, alias="nextOffset")

    class Config:
        populate_by_name = True


class ViewFacetValue(BaseModel):
    """A single facet value with its row count."""
    value: str
    count: int


class ViewFacetCreator(BaseModel):
    """A creator facet row, enriched with display metadata."""
    user_id: str = Field(alias="userId")
    display_name: str = Field(alias="displayName")
    email: Optional[str] = None
    count: int

    class Config:
        populate_by_name = True


class ViewCatalogStats(BaseModel):
    """Aggregate catalog counts surfaced in the Explorer stats bar.

    Returned by ``GET /api/v1/views/stats`` which accepts the same
    filter params as the list endpoint — so the numbers always
    describe the current query, not a stale "entire catalog" snapshot.
    """
    total: int
    recently_added: int = Field(alias="recentlyAdded")
    needs_attention: int = Field(alias="needsAttention")
    last_activity_at: Optional[str] = Field(None, alias="lastActivityAt")

    class Config:
        populate_by_name = True


class ViewFacetsResponse(BaseModel):
    """Aggregate facets across the views table.

    Used by the Explorer to populate the Tag / View Type / Creator
    dropdowns so users can pick from the real set of values in the
    database rather than a derived-from-current-page approximation.
    Facets are intentionally GLOBAL (not filter-scoped) so dropdowns
    always offer the full option space — see ``GET /views/stats`` for
    filter-aware counts.
    """
    tags: List[ViewFacetValue]
    view_types: List[ViewFacetValue] = Field(alias="viewTypes")
    creators: List[ViewFacetCreator]

    class Config:
        populate_by_name = True


# ─── Impact / Blast-radius models ─────────────────────────────────────────────

class ImpactedEntity(BaseModel):
    """A single entity (workspace, view, catalog item) affected by a deletion."""
    id: str
    name: str
    type: str  # e.g. "workspace", "view", "catalog_item"


class ProviderImpactResponse(BaseModel):
    """Blast-radius report when deleting a Provider."""
    catalogItems: List[ImpactedEntity] = []
    workspaces: List[ImpactedEntity] = []
    views: List[ImpactedEntity] = []


class WorkspaceDataSourceImpactResponse(BaseModel):
    """Blast-radius report when removing a Data Source from a Workspace."""
    views: List[ImpactedEntity] = []


# ─── Physical asset stats ──────────────────────────────────────────────────────

class PhysicalGraphStatsResponse(BaseModel):
    """Raw node/edge counts and type breakdowns for a physical graph/database."""
    nodeCount: int = 0
    edgeCount: int = 0
    entityTypeCounts: Dict[str, int] = {}
    edgeTypeCounts: Dict[str, int] = {}


# ============================================
# Announcement Models (global banners)
# ============================================

class AnnouncementCreateRequest(BaseModel):
    title: str
    message: str
    banner_type: str = Field(default="info", alias="bannerType")
    is_active: bool = Field(default=True, alias="isActive")
    snooze_duration_minutes: int = Field(default=0, alias="snoozeDurationMinutes")  # 0 = no snooze
    cta_text: Optional[str] = Field(None, alias="ctaText")
    cta_url: Optional[str] = Field(None, alias="ctaUrl")

    class Config:
        populate_by_name = True


class AnnouncementUpdateRequest(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    banner_type: Optional[str] = Field(None, alias="bannerType")
    is_active: Optional[bool] = Field(None, alias="isActive")
    snooze_duration_minutes: Optional[int] = Field(None, alias="snoozeDurationMinutes")
    cta_text: Optional[str] = Field(None, alias="ctaText")
    cta_url: Optional[str] = Field(None, alias="ctaUrl")

    class Config:
        populate_by_name = True


class AnnouncementResponse(BaseModel):
    id: str
    title: str
    message: str
    banner_type: str = Field(alias="bannerType")
    is_active: bool = Field(alias="isActive")
    snooze_duration_minutes: int = Field(0, alias="snoozeDurationMinutes")
    cta_text: Optional[str] = Field(None, alias="ctaText")
    cta_url: Optional[str] = Field(None, alias="ctaUrl")
    created_by: Optional[str] = Field(None, alias="createdBy")
    updated_by: Optional[str] = Field(None, alias="updatedBy")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


class AnnouncementConfigUpdateRequest(BaseModel):
    poll_interval_seconds: Optional[int] = Field(None, alias="pollIntervalSeconds")
    default_snooze_minutes: Optional[int] = Field(None, alias="defaultSnoozeMinutes")

    class Config:
        populate_by_name = True


class AnnouncementConfigResponse(BaseModel):
    poll_interval_seconds: int = Field(15, alias="pollIntervalSeconds")
    default_snooze_minutes: int = Field(30, alias="defaultSnoozeMinutes")
    updated_by: Optional[str] = Field(None, alias="updatedBy")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    class Config:
        populate_by_name = True


class AssetListResponse(BaseModel):
    """List of raw asset identifiers (graph names, database names, topics…) on a provider."""
    assets: List[str] = []


# ─── Enterprise Catalog models ─────────────────────────────────────────────────

class CatalogItemCreateRequest(BaseModel):
    provider_id: str = Field(alias="providerId")
    source_identifier: Optional[str] = Field(None, alias="sourceIdentifier")
    name: str
    description: Optional[str] = None
    permitted_workspaces: List[str] = Field(default_factory=lambda: ["*"], alias="permittedWorkspaces")

    class Config:
        populate_by_name = True


class CatalogItemUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    permitted_workspaces: Optional[List[str]] = Field(None, alias="permittedWorkspaces")

    class Config:
        populate_by_name = True


class CatalogItemResponse(BaseModel):
    id: str
    provider_id: str = Field(alias="providerId")
    source_identifier: Optional[str] = Field(None, alias="sourceIdentifier")
    name: str
    description: Optional[str] = None
    permitted_workspaces: List[str] = Field(default_factory=lambda: ["*"], alias="permittedWorkspaces")
    status: str = "active"
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True
