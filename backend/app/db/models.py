"""
SQLAlchemy ORM models for the management database.
All primary keys are text UUIDs. JSON columns stored as TEXT for SQLite compat.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .engine import Base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return f"conn_{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# graph_connections                                                     #
# ------------------------------------------------------------------ #

class GraphConnectionORM(Base):
    __tablename__ = "graph_connections"

    id = Column(Text, primary_key=True, default=_uuid)
    name = Column(Text, nullable=False)
    provider_type = Column(Text, nullable=False)      # falkordb | neo4j | datahub | mock
    host = Column(Text, nullable=True)
    port = Column(Integer, nullable=True)
    graph_name = Column(Text, nullable=True)
    credentials = Column(Text, nullable=True)         # Fernet-encrypted JSON blob
    tls_enabled = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    extra_config = Column(Text, nullable=True)        # JSON blob
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    # Relationships
    assignment_rule_sets = relationship(
        "AssignmentRuleSetORM", back_populates="connection",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_connections_provider_type", "provider_type"),
    )

    def __repr__(self) -> str:
        return f"<GraphConnection id={self.id!r} name={self.name!r} type={self.provider_type!r}>"


# ------------------------------------------------------------------ #
# assignment_rule_sets                                                  #
# ------------------------------------------------------------------ #

class AssignmentRuleSetORM(Base):
    __tablename__ = "assignment_rule_sets"

    id = Column(Text, primary_key=True, default=lambda: f"rs_{uuid.uuid4().hex[:12]}")
    connection_id = Column(
        Text,
        ForeignKey("graph_connections.id", ondelete="CASCADE"),
        nullable=True,  # nullable during migration
    )
    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,  # nullable during migration
    )
    data_source_id = Column(
        Text,
        ForeignKey("workspace_data_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    layers_config = Column(Text, nullable=False, default="[]")  # JSON
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    connection = relationship("GraphConnectionORM", back_populates="assignment_rule_sets")
    workspace = relationship(
        "WorkspaceORM", back_populates="assignment_rule_sets",
        foreign_keys=[workspace_id],
    )

    __table_args__ = (
        Index("idx_rule_sets_connection", "connection_id"),
        Index("idx_rule_sets_workspace", "workspace_id"),
        Index("idx_rule_sets_data_source", "data_source_id"),
    )

    def __repr__(self) -> str:
        return f"<AssignmentRuleSet id={self.id!r} name={self.name!r}>"


# ------------------------------------------------------------------ #
# view_favourites                                                      #
# ------------------------------------------------------------------ #

class ViewFavouriteORM(Base):
    __tablename__ = "view_favourites"

    id = Column(Text, primary_key=True, default=lambda: f"fav_{uuid.uuid4().hex[:12]}")
    view_id = Column(
        Text,
        ForeignKey("views.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False, default=_now)

    view = relationship("ViewORM", back_populates="favourites")

    __table_args__ = (
        UniqueConstraint("view_id", "user_id", name="uq_view_user_favourite"),
        Index("idx_favourites_user", "user_id"),
        Index("idx_favourites_view", "view_id"),
    )

    def __repr__(self) -> str:
        return f"<ViewFavourite view={self.view_id!r} user={self.user_id!r}>"


# ------------------------------------------------------------------ #
# management_db_config  (single-row config table)                      #
# ------------------------------------------------------------------ #

class ManagementDbConfigORM(Base):
    __tablename__ = "management_db_config"

    id = Column(Integer, primary_key=True, default=1)
    storage_backend = Column(Text, nullable=False, default="sqlite")
    falkordb_conn_id = Column(
        Text,
        ForeignKey("graph_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    falkordb_graph_name = Column(Text, nullable=True)
    postgres_url = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("id = 1", name="single_row_constraint"),
    )


# ------------------------------------------------------------------ #
# feature_categories  (definitions: id, label, icon, color, order)     #
# ------------------------------------------------------------------ #

class FeatureCategoryORM(Base):
    __tablename__ = "feature_categories"

    id = Column(Text, primary_key=True)
    label = Column(Text, nullable=False)
    icon = Column(Text, nullable=False)
    color = Column(Text, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    preview = Column(Boolean, nullable=False, default=True)  # show "not yet wired" badge and footer when True
    preview_label = Column(Text, nullable=True)  # e.g. "Not yet wired"
    preview_footer = Column(Text, nullable=True)  # footer text when preview=True


# ------------------------------------------------------------------ #
# feature_definitions  (definitions: key, name, type, default, etc.) #
# ------------------------------------------------------------------ #

class FeatureDefinitionORM(Base):
    __tablename__ = "feature_definitions"

    key = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    category_id = Column(Text, nullable=False)  # references feature_categories.id
    type = Column(Text, nullable=False)  # "boolean" | "string[]"
    default_value = Column(Text, nullable=False)  # JSON: true | false | ["graph",...]
    user_overridable = Column(Boolean, nullable=False, default=False)
    options = Column(Text, nullable=True)  # JSON: [{"id","label"},...] for string[]
    help_url = Column(Text, nullable=True)
    admin_hint = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    deprecated = Column(Boolean, nullable=False, default=False)
    implemented = Column(Boolean, nullable=False, default=False)  # when True, feature is "wired" (no preview badge)


# ------------------------------------------------------------------ #
# feature_registry_meta  (single-row: Admin Features UI copy)         #
# ------------------------------------------------------------------ #

class FeatureRegistryMetaORM(Base):
    __tablename__ = "feature_registry_meta"

    id = Column(Integer, primary_key=True, default=1)
    experimental_notice_enabled = Column(Boolean, nullable=False, default=True)
    experimental_notice_title = Column(Text, nullable=True)
    experimental_notice_message = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("id = 1", name="feature_registry_meta_single_row"),
    )


# ------------------------------------------------------------------ #
# feature_flags  (single-row config: global feature flag values)     #
# ------------------------------------------------------------------ #

class FeatureFlagsORM(Base):
    __tablename__ = "feature_flags"

    id = Column(Integer, primary_key=True, default=1)
    config = Column(Text, nullable=False, default="{}")  # JSON: { "editModeEnabled": true, ... }
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    version = Column(Integer, nullable=False, default=0)  # optimistic concurrency; incremented on every write

    __table_args__ = (
        CheckConstraint("id = 1", name="feature_flags_single_row"),
    )


# ------------------------------------------------------------------ #
# providers  (workspace-centric: pure infrastructure)                  #
# ------------------------------------------------------------------ #

class ProviderORM(Base):
    __tablename__ = "providers"

    id = Column(Text, primary_key=True, default=lambda: f"prov_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    provider_type = Column(Text, nullable=False)      # falkordb | neo4j | datahub | spanner | mock
    host = Column(Text, nullable=True)
    port = Column(Integer, nullable=True)
    credentials = Column(Text, nullable=True)         # Fernet-encrypted JSON blob
    tls_enabled = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    permitted_workspaces = Column(Text, nullable=False, default='["*"]')  # JSON list; "*" = all
    extra_config = Column(Text, nullable=True)        # JSON blob
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    # Relationships
    data_sources = relationship(
        "WorkspaceDataSourceORM", back_populates="provider",
        cascade="all, delete-orphan",
    )
    catalog_items = relationship(
        "CatalogItemORM", back_populates="provider",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_providers_type", "provider_type"),
        CheckConstraint(
            "provider_type IN ('falkordb', 'neo4j', 'datahub', 'spanner', 'mock')",
            name="ck_providers_provider_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<Provider id={self.id!r} name={self.name!r} type={self.provider_type!r}>"


# ------------------------------------------------------------------ #
# ontologies  (standalone, versioned, reusable semantic definitions)   #
# ------------------------------------------------------------------ #

class OntologyORM(Base):
    __tablename__ = "ontologies"

    id = Column(Text, primary_key=True, default=lambda: f"bp_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True, default=None)
    version = Column(Integer, nullable=False, default=1)
    # Legacy flat edge type lists (kept for backward compat; derived from definitions when present)
    containment_edge_types = Column(Text, nullable=False, default="[]")   # JSON
    lineage_edge_types = Column(Text, nullable=False, default="[]")       # JSON
    edge_type_metadata = Column(Text, nullable=False, default="{}")       # JSON
    entity_type_hierarchy = Column(Text, nullable=False, default="{}")    # JSON
    root_entity_types = Column(Text, nullable=False, default="[]")        # JSON
    # Rich definition columns (Phase 1+): nested dicts keyed by type ID
    entity_type_definitions = Column(Text, nullable=False, default="{}")  # JSON Dict[str, EntityTypeDefEntry]
    relationship_type_definitions = Column(Text, nullable=False, default="{}")  # JSON Dict[str, RelTypeDefEntry]
    # Ontology metadata
    is_published = Column(Boolean, nullable=False, default=False)
    is_system = Column(Boolean, nullable=False, default=False)
    scope = Column(Text, nullable=False, default="universal")             # universal | workspace
    # Schema evolution policy applied when a newer version of this ontology is published.
    # reject   — do not allow changes that would break existing data (safest).
    # deprecate — mark removed types as deprecated; continue to serve them.
    # migrate  — automatically rename/remap types per a migration manifest.
    evolution_policy = Column(Text, nullable=False, default="reject")   # reject | deprecate | migrate
    schema_id = Column(Text, nullable=False, default="")               # stable identifier grouping all versions
    revision = Column(Integer, nullable=False, default=0)              # optimistic locking counter
    created_by = Column(Text, nullable=True, default=None)             # who created this version
    updated_by = Column(Text, nullable=True, default=None)             # last modifier
    published_by = Column(Text, nullable=True, default=None)           # who published this version
    published_at = Column(Text, nullable=True, default=None)           # when published
    deleted_by = Column(Text, nullable=True, default=None)             # who soft-deleted
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(Text, nullable=True, default=None)             # soft delete timestamp

    # Relationships
    data_sources = relationship(
        "WorkspaceDataSourceORM", back_populates="ontology",
    )

    __table_args__ = (
        Index("idx_ontologies_name_version", "name", "version"),
        Index("idx_ontologies_is_system", "is_system"),
        Index("idx_ontologies_schema_id", "schema_id"),
        Index("idx_ontologies_deleted", "deleted_at"),
        CheckConstraint(
            "scope IN ('universal', 'workspace')",
            name="ck_ontologies_scope",
        ),
        CheckConstraint(
            "evolution_policy IN ('reject', 'deprecate', 'migrate')",
            name="ck_ontologies_evolution_policy",
        ),
    )

    def __repr__(self) -> str:
        return f"<Ontology id={self.id!r} name={self.name!r} v{self.version}>"


# ------------------------------------------------------------------ #
# ontology_audit_log — immutable trail of ontology lifecycle events    #
# ------------------------------------------------------------------ #

class OntologyAuditLogORM(Base):
    """
    Immutable audit trail for ontology lifecycle events.
    Each row captures a single action (create, update, publish, delete, restore, clone)
    along with who performed it and a summary of what changed.
    """
    __tablename__ = "ontology_audit_log"

    id = Column(Text, primary_key=True, default=lambda: f"oal_{uuid.uuid4().hex[:12]}")
    ontology_id = Column(Text, nullable=False, index=True)
    schema_id = Column(Text, nullable=False, index=True)           # groups events across versions
    action = Column(Text, nullable=False)                           # created | updated | published | deleted | restored | cloned
    actor = Column(Text, nullable=True)                             # user who performed the action
    version = Column(Integer, nullable=True)                        # ontology version at time of action
    summary = Column(Text, nullable=True)                           # human-readable summary
    changes = Column(Text, nullable=True, default=None)             # JSON: detailed diff (added/removed types, changed fields)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_oal_ontology", "ontology_id"),
        Index("idx_oal_schema", "schema_id"),
        Index("idx_oal_created", "created_at"),
        Index("idx_oal_actor_action", "actor", "action", "created_at"),
        CheckConstraint(
            "action IN ('created', 'updated', 'published', 'deleted', 'restored', 'cloned')",
            name="ck_oal_action_enum",
        ),
    )

    def __repr__(self) -> str:
        return f"<OntologyAuditLog id={self.id!r} action={self.action!r} ontology={self.ontology_id!r}>"


# ------------------------------------------------------------------ #
# ontology_source_mappings — per-source type mapping profiles          #
# ------------------------------------------------------------------ #

class OntologySourceMappingORM(Base):
    """
    Maps external provider type labels to Synodic ontology type IDs.

    When a DataHub asset arrives with type "DATASET" from platform "snowflake",
    the mapping profile for that data source translates it to the Synodic
    entity type "dataset" before writing to the graph.

    One row per (data_source_id, external_type) pair.
    entity_type_mappings and relationship_type_mappings are JSON dicts:
      { "<external_label>": "<synodic_type_id>", … }
    """
    __tablename__ = "ontology_source_mappings"

    id = Column(Text, primary_key=True, default=lambda: f"osm_{uuid.uuid4().hex[:12]}")
    data_source_id = Column(Text, nullable=False, index=True)
    ontology_id = Column(Text, nullable=True)                        # optional pinned ontology
    # JSON dict: external entity type label → Synodic entity type id
    entity_type_mappings = Column(Text, nullable=False, default="{}")
    # JSON dict: external relationship type label → Synodic relationship type id
    relationship_type_mappings = Column(Text, nullable=False, default="{}")
    # EXTENSION POINT: add conditional aliasing/ignore rules when DataHub/OpenMetadata
    # ingestion needs source-context-aware mappings beyond simple label->type maps.
    # Snapshot of the last-seen external schema (for drift detection)
    last_seen_schema_hash = Column(Text, nullable=True)
    last_seen_at = Column(Text, nullable=True)
    # Whether the last drift check found unmapped types
    has_drift = Column(Boolean, nullable=False, default=False)
    drift_details = Column(Text, nullable=True)                      # JSON list of issues
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_osm_data_source", "data_source_id"),
        Index("idx_osm_ontology", "ontology_id"),
    )

    def __repr__(self) -> str:
        return f"<OntologySourceMapping ds={self.data_source_id!r}>"


# ------------------------------------------------------------------ #
# workspaces  (operational context — a team's "project")               #
# ------------------------------------------------------------------ #

class WorkspaceORM(Base):
    __tablename__ = "workspaces"

    id = Column(Text, primary_key=True, default=lambda: f"ws_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Audit-only attribution; does not grant any permission. Resolved
    # access lives in role_bindings.
    created_by = Column(Text, nullable=True, default=None)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    # Soft delete (Phase 1 policy: user-visible + cross-referenced → soft)
    deleted_at = Column(Text, nullable=True, default=None)
    deleted_by = Column(Text, nullable=True, default=None)

    # Relationships
    data_sources = relationship(
        "WorkspaceDataSourceORM", back_populates="workspace",
        cascade="all, delete-orphan",
    )
    assignment_rule_sets = relationship(
        "AssignmentRuleSetORM", back_populates="workspace",
        foreign_keys="AssignmentRuleSetORM.workspace_id",
    )

    __table_args__ = (
        Index("idx_workspaces_is_default", "is_default"),
        Index("idx_workspaces_deleted_at", "deleted_at"),
    )

    def __repr__(self) -> str:
        return f"<Workspace id={self.id!r} name={self.name!r}>"


# ------------------------------------------------------------------ #
# workspace_data_sources  (binds provider + graph + assigned ontology)  #
# ------------------------------------------------------------------ #

class WorkspaceDataSourceORM(Base):
    __tablename__ = "workspace_data_sources"

    id = Column(Text, primary_key=True, default=lambda: f"ds_{uuid.uuid4().hex[:12]}")
    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id = Column(
        Text,
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    graph_name = Column(Text, nullable=True)
    catalog_item_id = Column(
        Text,
        ForeignKey("catalog_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    ontology_id = Column(
        Text,
        ForeignKey("ontologies.id", ondelete="SET NULL"),
        nullable=True,
    )
    label = Column(Text, nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    projection_mode = Column(Text, nullable=True)  # None = inherit from provider, "in_source" | "dedicated"
    dedicated_graph_name = Column(Text, nullable=True)  # graph name when projection_mode == "dedicated"
    access_level = Column(Text, nullable=True, default="read")  # read | write | admin
    extra_config = Column(Text, nullable=True)  # JSON — per-data-source config (schema mapping overrides, etc.)
    # ── Aggregation state ─────────────────────────────────────
    aggregation_status = Column(Text, nullable=False, default="none")  # none|pending|running|ready|failed|skipped
    last_aggregated_at = Column(Text, nullable=True)  # ISO timestamp of last successful aggregation
    aggregation_edge_count = Column(Integer, nullable=False, default=0)  # count of AGGREGATED edges created
    graph_fingerprint = Column(Text, nullable=True)  # JSON hash of node/edge counts by type (change detection)
    aggregation_schedule = Column(Text, nullable=True)  # Cron expression (e.g., "0 */6 * * *") for periodic checks
    # Audit-only attribution; does not grant any permission.
    created_by = Column(Text, nullable=True, default=None)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    # Soft delete (Phase 1 policy: user-visible + cross-referenced → soft)
    deleted_at = Column(Text, nullable=True, default=None)
    deleted_by = Column(Text, nullable=True, default=None)

    # Relationships
    workspace = relationship("WorkspaceORM", back_populates="data_sources")
    provider = relationship("ProviderORM", back_populates="data_sources")
    catalog_item = relationship("CatalogItemORM")
    ontology = relationship("OntologyORM", back_populates="data_sources")
    # EXTENSION POINT: add ontology_version_strategy (pinned|floating) and
    # ontology_enforcement (permissive|strict) when multi-source governance
    # requires per-data-source resolution policies.
    stats = relationship("DataSourceStatsORM", back_populates="data_source", uselist=False, cascade="all, delete-orphan")
    polling_config = relationship("DataSourcePollingConfigORM", back_populates="data_source", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("workspace_id", "provider_id", "graph_name", name="uq_ds_ws_prov_graph"),
        UniqueConstraint("catalog_item_id", name="uq_ds_catalog_item"),
        Index("idx_ds_workspace", "workspace_id"),
        Index("idx_ds_provider", "provider_id"),
        Index("idx_ds_catalog_item", "catalog_item_id"),
        Index("idx_ds_ontology", "ontology_id"),
        Index("idx_ds_deleted_at", "deleted_at"),
        CheckConstraint(
            "aggregation_status IN ('none', 'pending', 'running', 'ready', 'failed', 'skipped')",
            name="ck_ds_aggregation_status",
        ),
        CheckConstraint(
            "access_level IS NULL OR access_level IN ('read', 'write', 'admin')",
            name="ck_ds_access_level",
        ),
        CheckConstraint(
            "projection_mode IS NULL OR projection_mode IN ('in_source', 'dedicated')",
            name="ck_ds_projection_mode",
        ),
    )


# ------------------------------------------------------------------ #
# context_models  (how to visualize/organize the graph)               #
# ------------------------------------------------------------------ #

class ContextModelORM(Base):
    __tablename__ = "context_models"

    id = Column(Text, primary_key=True, default=lambda: f"cm_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,  # null = global template
    )
    data_source_id = Column(
        Text,
        ForeignKey("workspace_data_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_template = Column(Boolean, nullable=False, default=False)
    category = Column(Text, nullable=True)                           # e.g. "data-engineering"
    layers_config = Column(Text, nullable=False, default="[]")       # JSON: ViewLayerConfig[]
    scope_filter = Column(Text, nullable=True)                       # JSON: ScopeFilterConfig
    instance_assignments = Column(Text, nullable=False, default="{}") # JSON: entityId→assignment
    scope_edge_config = Column(Text, nullable=True)                  # JSON: ScopeEdgeConfig
    is_active = Column(Boolean, nullable=False, default=True)
    # Columns added during context-model → view unification
    view_type = Column(Text, nullable=True)                            # graph | table | lineage | ...
    config = Column(Text, nullable=True)                               # JSON: full ViewConfiguration
    visibility = Column(Text, nullable=False, default="private")       # private | workspace | enterprise
    created_by = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)                                 # JSON array
    is_pinned = Column(Boolean, nullable=False, default=False)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    # Relationships
    workspace = relationship(
        "WorkspaceORM",
        foreign_keys=[workspace_id],
    )

    __table_args__ = (
        Index("idx_cm_workspace", "workspace_id"),
        Index("idx_cm_template", "is_template"),
        CheckConstraint(
            "visibility IN ('private', 'workspace', 'enterprise')",
            name="ck_context_models_visibility",
        ),
    )


# ------------------------------------------------------------------ #
# views (Visual rendering of context models)                           #
# ------------------------------------------------------------------ #

class ViewORM(Base):
    __tablename__ = "views"

    id = Column(Text, primary_key=True, default=lambda: f"view_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    context_model_id = Column(
        Text,
        ForeignKey("context_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    data_source_id = Column(
        Text,
        ForeignKey("workspace_data_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    view_type = Column(Text, nullable=False, default="graph")
    config = Column(Text, nullable=False, default="{}")       # JSON: full ViewConfiguration
    # Ontology digest captured at view save time — used by the wizard to
    # detect drift when a user edits a view whose ontology has changed since
    # creation. Nullable because legacy rows pre-date the column; the wizard
    # treats NULL as "drift check unavailable" (no warning shown).
    ontology_digest = Column(Text, nullable=True, default=None)
    # EXTENSION POINT: persist referenced_entity_types / referenced_relationship_types
    # for view-ontology compatibility checks once real breakage workflows appear.
    visibility = Column(Text, nullable=False, default="private")
    created_by = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)                        # JSON array
    is_pinned = Column(Boolean, nullable=False, default=False)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(Text, nullable=True, default=None)

    # Relationships
    context_model = relationship("ContextModelORM", backref="views")
    workspace = relationship("WorkspaceORM", foreign_keys=[workspace_id])
    favourites = relationship("ViewFavouriteORM", back_populates="view", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_view_workspace", "workspace_id"),
        Index("idx_view_context_model", "context_model_id"),
        Index("idx_view_visibility", "visibility"),
        Index("idx_view_data_source", "data_source_id"),
        Index("idx_view_deleted_at", "deleted_at"),
        CheckConstraint(
            "visibility IN ('private', 'workspace', 'enterprise')",
            name="ck_views_visibility",
        ),
    )

    def __repr__(self) -> str:
        return f"<View id={self.id!r} name={self.name!r} type={self.view_type!r}>"


# ------------------------------------------------------------------ #
# data_source_stats (Graph Statistics Cache)                           #
# ------------------------------------------------------------------ #

class DataSourceStatsORM(Base):
    __tablename__ = "data_source_stats"

    data_source_id = Column(
        Text,
        ForeignKey("workspace_data_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    node_count = Column(Integer, nullable=False, default=0)
    edge_count = Column(Integer, nullable=False, default=0)
    entity_type_counts = Column(Text, nullable=False, default="{}")  # JSON
    edge_type_counts = Column(Text, nullable=False, default="{}")    # JSON
    schema_stats = Column(Text, nullable=False, default="{}")        # JSON
    ontology_metadata = Column(Text, nullable=False, default="{}")   # JSON
    graph_schema = Column(Text, nullable=False, default="{}")        # JSON
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    # Relationships
    data_source = relationship("WorkspaceDataSourceORM", back_populates="stats")

    def __repr__(self) -> str:
        return f"<DataSourceStats ds_id={self.data_source_id!r}>"


# ------------------------------------------------------------------ #
# data_source_polling_configs (Microservice orchestration)             #
# ------------------------------------------------------------------ #

class DataSourcePollingConfigORM(Base):
    __tablename__ = "data_source_polling_configs"

    data_source_id = Column(
        Text,
        ForeignKey("workspace_data_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_enabled = Column(Boolean, nullable=False, default=True)
    interval_seconds = Column(Integer, nullable=False, default=300)
    last_polled_at = Column(Text, nullable=True)                     # ISO string
    last_status = Column(Text, nullable=False, default="pending")    # pending | success | error
    last_error = Column(Text, nullable=True)

    # Relationships
    data_source = relationship("WorkspaceDataSourceORM", back_populates="polling_config")

    __table_args__ = (
        CheckConstraint(
            "last_status IN ('pending', 'success', 'error')",
            name="ck_polling_last_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<DataSourcePollingConfig ds_id={self.data_source_id!r} enabled={self.is_enabled}>"


# ------------------------------------------------------------------ #
# asset_discovery_cache (Pre-registration asset cache)                 #
# ------------------------------------------------------------------ #
# Caches the result of provider asset-list and per-asset stats calls
# made during onboarding (before a workspace_data_source exists). Keyed
# by (provider_id, asset_name); the empty-string asset_name is the
# sentinel row for the "list all assets on this provider" payload.

class AssetDiscoveryCacheORM(Base):
    __tablename__ = "asset_discovery_cache"

    provider_id = Column(
        Text,
        ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    asset_name = Column(Text, primary_key=True, default="")
    payload = Column(Text, nullable=False, default="{}")  # JSON
    status = Column(Text, nullable=False, default="fresh")  # fresh | stale | partial
    computed_at = Column(Text, nullable=False, default=_now)
    expires_at = Column(Text, nullable=False)  # ISO; absolute, cleaned by scheduler
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_adc_expires", "expires_at"),
        CheckConstraint(
            "status IN ('fresh', 'stale', 'partial')",
            name="ck_adc_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<AssetDiscoveryCache provider={self.provider_id!r} asset={self.asset_name!r} status={self.status!r}>"


# ------------------------------------------------------------------ #
# provider_admission_config (Per-provider rate-limit knobs)            #
# ------------------------------------------------------------------ #
# Admin-tunable token-bucket + circuit-breaker parameters per provider.
# Read by insights_service workers on each job; absence of a row falls
# back to module defaults (see backend/insights_service/admission.py).

class ProviderAdmissionConfigORM(Base):
    __tablename__ = "provider_admission_config"

    provider_id = Column(
        Text,
        ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bucket_capacity = Column(Integer, nullable=False, default=8)
    refill_per_sec = Column(Integer, nullable=False, default=2)
    circuit_fail_max = Column(Integer, nullable=False, default=5)
    circuit_window_secs = Column(Integer, nullable=False, default=30)
    half_open_after_secs = Column(Integer, nullable=False, default=60)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<ProviderAdmissionConfig provider={self.provider_id!r} cap={self.bucket_capacity} refill={self.refill_per_sec}/s>"


# ------------------------------------------------------------------ #
# provider_health_window (Rolling success window)                      #
# ------------------------------------------------------------------ #
# Worker-maintained rolling-window counters for admission control.
# `throttle_until` is set when the rolling success rate drops below
# threshold; while in the future, workers defer enqueues for this
# provider rather than burning capacity.

class ProviderHealthWindowORM(Base):
    __tablename__ = "provider_health_window"

    provider_id = Column(
        Text,
        ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    window_start = Column(Text, nullable=False, default=_now)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    throttle_until = Column(Text, nullable=True)
    last_p99_ms = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<ProviderHealthWindow provider={self.provider_id!r} ok={self.success_count} fail={self.failure_count}>"


# ------------------------------------------------------------------ #
# catalog_items  (enterprise data asset catalog)                       #
# ------------------------------------------------------------------ #

class CatalogItemORM(Base):
    """
    Maps a named physical asset (e.g. a graph within a FalkorDB provider)
    to a managed, permission-controlled catalog entry.
    Workspaces consume catalog items instead of talking directly to providers.
    """
    __tablename__ = "catalog_items"

    id = Column(Text, primary_key=True, default=lambda: f"cat_{uuid.uuid4().hex[:12]}")
    provider_id = Column(
        Text,
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_identifier = Column(Text, nullable=True)  # e.g. the graph name on the provider
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    permitted_workspaces = Column(Text, nullable=False, default='["*"]')  # JSON list; "*" = all
    status = Column(Text, nullable=False, default="active")  # active | archived | deprecated
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    # Relationships
    provider = relationship("ProviderORM", back_populates="catalog_items")

    __table_args__ = (
        UniqueConstraint("provider_id", "source_identifier", name="uq_catalog_provider_source"),
        Index("idx_catalog_provider", "provider_id"),
        Index("idx_catalog_status", "status"),
        CheckConstraint(
            "status IN ('active', 'archived', 'deprecated')",
            name="ck_catalog_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<CatalogItem id={self.id!r} name={self.name!r} provider={self.provider_id!r}>"


# ------------------------------------------------------------------ #
# users  (authentication & identity)                                   #
# ------------------------------------------------------------------ #

class UserORM(Base):
    __tablename__ = "users"

    id = Column(Text, primary_key=True, default=lambda: f"usr_{uuid.uuid4().hex[:12]}")
    email = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)
    first_name = Column(Text, nullable=False)
    last_name = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending")       # pending | active | suspended
    auth_provider = Column(Text, nullable=False, default="local")  # local | saml2 | oidc
    external_id = Column(Text, nullable=True)                      # SSO subject
    metadata_ = Column("metadata", Text, nullable=True, default="{}")  # JSON: SSO claims, prefs
    reset_token_hash = Column(Text, nullable=True)
    reset_token_expires_at = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(Text, nullable=True)                       # soft delete

    # Relationships
    roles = relationship("UserRoleORM", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        # SSO identity join key. Local rows leave external_id NULL and
        # NULLs are distinct under a UNIQUE constraint, so this only
        # constrains provisioned SSO subjects (one row per
        # provider+subject) without affecting local accounts.
        UniqueConstraint(
            "auth_provider", "external_id",
            name="uq_users_provider_external_id",
        ),
        Index("idx_users_status_created", "status", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'active', 'suspended')",
            name="ck_users_status",
        ),
        CheckConstraint(
            "auth_provider IN ('local', 'saml2', 'oidc')",
            name="ck_users_auth_provider",
        ),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} email={self.email!r} status={self.status!r}>"


# ------------------------------------------------------------------ #
# user_roles  (one row per user × role)                                #
# ------------------------------------------------------------------ #

class UserRoleORM(Base):
    __tablename__ = "user_roles"

    id = Column(Text, primary_key=True, default=lambda: f"urole_{uuid.uuid4().hex[:12]}")
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role_name = Column(Text, nullable=False, default="user")  # admin | user | viewer
    created_at = Column(Text, nullable=False, default=_now)

    user = relationship("UserORM", back_populates="roles")

    __table_args__ = (
        UniqueConstraint("user_id", "role_name", name="uq_user_role"),
        Index("idx_user_roles_user", "user_id"),
        CheckConstraint(
            "role_name IN ('admin', 'user', 'viewer')",
            name="ck_user_roles_role_name",
        ),
    )

    def __repr__(self) -> str:
        return f"<UserRole user={self.user_id!r} role={self.role_name!r}>"


# ------------------------------------------------------------------ #
# user_approvals  (audit trail for signup approval / rejection)        #
# ------------------------------------------------------------------ #

class UserApprovalORM(Base):
    __tablename__ = "user_approvals"

    id = Column(Text, primary_key=True, default=lambda: f"uapr_{uuid.uuid4().hex[:12]}")
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    approved_by = Column(Text, nullable=True)                     # admin user_id (logical ref)
    status = Column(Text, nullable=False, default="pending")      # pending | approved | rejected
    rejection_reason = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    resolved_at = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_user_approvals_user_status", "user_id", "status"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_user_approvals_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<UserApproval user={self.user_id!r} status={self.status!r}>"


# ====================================================================== #
# RBAC — Subject-Role-Scope binding model                                  #
# ====================================================================== #
# Six tables implement the binding model documented in the Enterprise
# RBAC plan (docs/superpowers/specs/...). The shape is:
#
#     subject (user|group)  ──╮
#                              ├── role_binding ──> role ──> permissions
#     scope   (global|ws_*)  ──╯
#
# Resource-level grants on Views (the only resource that supports
# explicit per-row sharing in Phase 1) live in ``resource_grants``.
# Data Sources are workspace-inherited and have no explicit grants.

# ------------------------------------------------------------------ #
# permissions  (catalogue of every permission the system enforces)     #
# ------------------------------------------------------------------ #

class PermissionORM(Base):
    """Single permission identifier and human description.

    The id is a stable string like ``workspace:view:edit`` — chosen
    deliberately so role_permissions rows and JWT claims can reference
    the permission by name, not by surrogate key. Seeded once by the
    Phase 1 migration; not user-editable in Phase 1.

    Phase 4.1 added ``long_description`` (paragraph-form explanation
    surfaced in the admin UI tooltip) and ``examples`` (JSON-encoded
    list of concrete example actions). Both backfilled by the
    ``20260430_1700_permission_descriptions`` migration.
    """
    __tablename__ = "permissions"

    id = Column(Text, primary_key=True)        # e.g. "workspace:view:edit"
    description = Column(Text, nullable=False)
    category = Column(Text, nullable=False)    # system | workspace | resource
    long_description = Column(Text, nullable=True)
    examples = Column(Text, nullable=True)     # JSON-encoded list[str]

    __table_args__ = (
        CheckConstraint(
            "category IN ('system', 'workspace', 'resource')",
            name="ck_permissions_category",
        ),
    )

    def __repr__(self) -> str:
        return f"<Permission id={self.id!r} category={self.category!r}>"


# ------------------------------------------------------------------ #
# role_permissions  (which permissions belong to which role)           #
# ------------------------------------------------------------------ #

class RolePermissionORM(Base):
    """Role → permission mapping.

    Phase 3 promoted ``role_name`` from a CHECK-constrained enum into
    a foreign-key-ish reference to the canonical ``roles`` table. The
    DB-level CHECK on the role name is dropped by the
    ``20260430_1500_roles_lifecycle`` migration; ``role_repo`` enforces
    referential integrity at the application boundary so a future
    Postgres-native FK is a non-breaking change.
    """
    __tablename__ = "role_permissions"

    role_name = Column(Text, primary_key=True)
    permission_id = Column(
        Text,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )

    __table_args__ = (
        Index("idx_role_permissions_role", "role_name"),
    )

    def __repr__(self) -> str:
        return f"<RolePermission role={self.role_name!r} perm={self.permission_id!r}>"


# ------------------------------------------------------------------ #
# roles  (canonical role definitions — Phase 3 lifecycle)              #
# ------------------------------------------------------------------ #

class RoleORM(Base):
    """A role definition.

    Phase 1 baked admin/user/viewer into CHECK constraints on the
    role-permission and role-binding tables. Phase 3 promotes the
    role name into a real entity so:

      * Custom roles can be created and edited in the admin UI.
      * Each role can be ``global`` (usable in any binding) or
        ``workspace``-scoped (only assignable inside that workspace).
      * The ``is_system`` flag marks built-in roles that the UI
        renders read-only.

    Application-level guards in ``role_repo`` and ``binding_repo``
    enforce that bindings can only reference a role whose scope
    matches the binding's scope (a workspace-scoped role cannot be
    bound globally, etc.).
    """
    __tablename__ = "roles"

    name = Column(Text, primary_key=True)
    description = Column(Text, nullable=True)
    scope_type = Column(Text, nullable=False, default="global")  # global | workspace
    scope_id = Column(Text, nullable=True)
    is_system = Column(Boolean, nullable=False, default=False)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    created_by = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_roles_scope", "scope_type", "scope_id"),
        Index("idx_roles_is_system", "is_system"),
        CheckConstraint(
            "scope_type IN ('global', 'workspace')",
            name="ck_roles_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) "
            "OR (scope_type = 'workspace' AND scope_id IS NOT NULL)",
            name="ck_roles_scope_consistency",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Role name={self.name!r} scope={self.scope_type}:{self.scope_id} "
            f"system={self.is_system}>"
        )


# ------------------------------------------------------------------ #
# groups  (named collections of users — the second Subject type)       #
# ------------------------------------------------------------------ #

class GroupORM(Base):
    """A named group of users.

    Groups are global (not workspace-scoped) so the same group can be
    bound to many workspaces with different roles, matching how
    Okta/Entra/SCIM directories work.
    """
    __tablename__ = "groups"

    id = Column(Text, primary_key=True, default=lambda: f"grp_{uuid.uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    # Provenance: 'local' = created in-app; 'scim' = synced from an
    # external IdP. external_id is the SCIM subject when source='scim'.
    # These two columns are placeholders for Phase 2 SSO sync.
    source = Column(Text, nullable=False, default="local")
    external_id = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(Text, nullable=True, default=None)

    members = relationship(
        "GroupMemberORM", back_populates="group",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_groups_name"),
        Index("idx_groups_deleted_at", "deleted_at"),
        Index("idx_groups_external_id", "external_id"),
        CheckConstraint(
            "source IN ('local', 'scim')",
            name="ck_groups_source",
        ),
    )

    def __repr__(self) -> str:
        return f"<Group id={self.id!r} name={self.name!r}>"


# ------------------------------------------------------------------ #
# group_members  (user × group membership)                             #
# ------------------------------------------------------------------ #

class GroupMemberORM(Base):
    __tablename__ = "group_members"

    group_id = Column(
        Text,
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(
        Text,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at = Column(Text, nullable=False, default=_now)
    added_by = Column(Text, nullable=True)

    group = relationship("GroupORM", back_populates="members")

    __table_args__ = (
        # Hot path: "what groups is this user in?" — used by the
        # PermissionResolver on every login.
        Index("idx_group_members_user", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<GroupMember group={self.group_id!r} user={self.user_id!r}>"


# ------------------------------------------------------------------ #
# role_bindings  (the central binding table)                           #
# ------------------------------------------------------------------ #

class RoleBindingORM(Base):
    """Binds a Subject (user|group) to a Role within a Scope (global|workspace).

    No FK on subject_id — it's polymorphic (users.id OR groups.id).
    Referential integrity is enforced in repository code; orphaned
    bindings are pruned by the on-delete handlers on users/groups.

    No FK on scope_id either: it's NULL for global scope and references
    workspaces.id for workspace scope. CASCADE deletion of workspace_id
    bindings is handled in repository code (Phase 2 wires the
    workspace-delete event handler to revoke and remove bindings).
    """
    __tablename__ = "role_bindings"

    id = Column(Text, primary_key=True, default=lambda: f"bnd_{uuid.uuid4().hex[:12]}")
    subject_type = Column(Text, nullable=False)   # user | group
    subject_id = Column(Text, nullable=False)
    role_name = Column(Text, nullable=False)
    scope_type = Column(Text, nullable=False)     # global | workspace
    scope_id = Column(Text, nullable=True)        # NULL for global
    granted_at = Column(Text, nullable=False, default=_now)
    granted_by = Column(Text, nullable=True)      # user_id who created the binding
    # Time-bound bindings: schema-ready in Phase 1, not enforced until Phase 2.
    expires_at = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "subject_type", "subject_id", "role_name", "scope_type", "scope_id",
            name="uq_role_binding",
        ),
        # Hot path: PermissionResolver pulls all bindings for a subject.
        Index("idx_role_bindings_subject", "subject_id", "scope_type", "scope_id"),
        # Reverse lookup: "who has access to this workspace?"
        Index("idx_role_bindings_scope", "scope_type", "scope_id"),
        Index("idx_role_bindings_role", "role_name"),
        CheckConstraint(
            "subject_type IN ('user', 'group')",
            name="ck_role_bindings_subject_type",
        ),
        CheckConstraint(
            "scope_type IN ('global', 'workspace')",
            name="ck_role_bindings_scope_type",
        ),
        CheckConstraint(
            # Global scope has NULL scope_id; workspace scope has a value.
            "(scope_type = 'global' AND scope_id IS NULL) "
            "OR (scope_type = 'workspace' AND scope_id IS NOT NULL)",
            name="ck_role_bindings_scope_consistency",
        ),
        # Phase 3 dropped the role_name CHECK constraint — the canonical
        # ``roles`` table is now the source of truth and ``role_repo``
        # enforces referential integrity in app code.
    )

    def __repr__(self) -> str:
        return (
            f"<RoleBinding id={self.id!r} "
            f"{self.subject_type}={self.subject_id!r} "
            f"role={self.role_name!r} "
            f"{self.scope_type}={self.scope_id!r}>"
        )


# ------------------------------------------------------------------ #
# resource_grants  (per-View explicit shares — Layer 3 of view ACL)    #
# ------------------------------------------------------------------ #

class ResourceGrantORM(Base):
    """Explicit grant of access to a single resource (Phase 1: views only).

    Additive only — a grant extends access to a subject regardless of
    workspace membership. The role_name enum here is intentionally
    narrower than the global role enum: only 'editor' or 'viewer' make
    sense at the resource level. It is NOT FK'd to role_permissions.
    """
    __tablename__ = "resource_grants"

    id = Column(Text, primary_key=True, default=lambda: f"grt_{uuid.uuid4().hex[:12]}")
    resource_type = Column(Text, nullable=False)  # 'view' for now
    resource_id = Column(Text, nullable=False)
    subject_type = Column(Text, nullable=False)   # user | group
    subject_id = Column(Text, nullable=False)
    role_name = Column(Text, nullable=False)      # editor | viewer (narrow)
    granted_at = Column(Text, nullable=False, default=_now)
    granted_by = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "resource_type", "resource_id", "subject_type", "subject_id",
            name="uq_resource_grant_subject",
        ),
        # Hot path: "what grants exist on this view?"
        Index("idx_resource_grants_resource", "resource_type", "resource_id"),
        # Reverse: "what views does Bob have explicit access to?"
        Index("idx_resource_grants_subject", "subject_type", "subject_id"),
        CheckConstraint(
            "resource_type IN ('view')",
            name="ck_resource_grants_resource_type",
        ),
        CheckConstraint(
            "subject_type IN ('user', 'group')",
            name="ck_resource_grants_subject_type",
        ),
        CheckConstraint(
            "role_name IN ('editor', 'viewer')",
            name="ck_resource_grants_role_name",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ResourceGrant id={self.id!r} "
            f"{self.resource_type}={self.resource_id!r} "
            f"{self.subject_type}={self.subject_id!r} "
            f"role={self.role_name!r}>"
        )


# ------------------------------------------------------------------ #
# access_requests  (Phase 4.3 — self-service workspace access asks)    #
# ------------------------------------------------------------------ #

class AccessRequestORM(Base):
    """A user asking a workspace admin for access at a specific role.

    State machine: ``pending`` → ``approved`` | ``denied``. Approval
    atomically creates the corresponding role binding (handled in the
    endpoint). The row stays around in either resolved state so the
    requester can see the resolution + reason on their My Access page.

    No FKs on ``requester_id`` / ``target_id`` — they reference users
    and workspaces respectively but rely on application-level guards
    (and on-delete cascades) the same way ``role_bindings`` does. The
    matching ``role_bindings`` row is the one that actually grants
    access; this table is metadata about the *ask*.
    """
    __tablename__ = "access_requests"

    id = Column(Text, primary_key=True, default=lambda: f"req_{uuid.uuid4().hex[:12]}")
    requester_id = Column(Text, nullable=False)
    target_type = Column(Text, nullable=False)        # only 'workspace' for now
    target_id = Column(Text, nullable=False)
    requested_role = Column(Text, nullable=False)     # must exist in roles table
    justification = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="pending")
    created_at = Column(Text, nullable=False, default=_now)
    resolved_at = Column(Text, nullable=True)
    resolved_by = Column(Text, nullable=True)
    resolution_note = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_access_requests_target_status",
            "target_type", "target_id", "status",
        ),
        Index(
            "idx_access_requests_requester_status",
            "requester_id", "status",
        ),
        CheckConstraint(
            "target_type IN ('workspace')",
            name="ck_access_requests_target_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied')",
            name="ck_access_requests_status",
        ),
        CheckConstraint(
            # Pending rows have no resolution; resolved rows have both
            # a timestamp and (usually) a resolver id.
            "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL) "
            "OR (status IN ('approved', 'denied') AND resolved_at IS NOT NULL)",
            name="ck_access_requests_state_consistency",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AccessRequest id={self.id!r} "
            f"requester={self.requester_id!r} "
            f"{self.target_type}={self.target_id!r} "
            f"role={self.requested_role!r} "
            f"status={self.status!r}>"
        )


# ------------------------------------------------------------------ #
# revoked_refresh_jti  (refresh-token rotation tracking)               #
# ------------------------------------------------------------------ #
# Each row records a refresh-token jti that has been consumed (rotated)
# or revoked (logout / reuse-detection). The auth service consults this
# table to:
#   1. Detect refresh-token replay (presented jti already recorded → kill family)
#   2. Honour explicit revocations (whole-family entries from logout)
# Owned by the auth service; will move with it during extraction.

class RevokedRefreshJtiORM(Base):
    __tablename__ = "revoked_refresh_jti"

    jti = Column(Text, primary_key=True)
    family_id = Column(Text, nullable=False)
    revoked_at = Column(Text, nullable=False, default=_now)
    expires_at = Column(Text, nullable=False)  # ISO; rows past this can be GC'd

    __table_args__ = (
        Index("idx_revoked_refresh_family", "family_id"),
        Index("idx_revoked_refresh_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<RevokedRefreshJti jti={self.jti!r} family={self.family_id!r}>"


# ------------------------------------------------------------------ #
# outbox_events  (transactional outbox for domain events)              #
# ------------------------------------------------------------------ #

class AnnouncementORM(Base):
    __tablename__ = "announcements"

    id = Column(Text, primary_key=True, default=lambda: f"ann_{uuid.uuid4().hex[:12]}")
    title = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    banner_type = Column(Text, nullable=False, default="info")        # info | warning | success
    is_active = Column(Boolean, nullable=False, default=True)
    is_dismissible = Column(Boolean, nullable=False, default=True)        # legacy; kept for DB compat
    snooze_duration_minutes = Column(Integer, nullable=False, default=0)  # 0 = no snooze allowed
    cta_text = Column(Text, nullable=True)                            # call-to-action button label
    cta_url = Column(Text, nullable=True)                             # call-to-action URL
    created_by = Column(Text, nullable=True)                          # admin user_id who created
    updated_by = Column(Text, nullable=True)                          # admin user_id who last updated
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_announcements_is_active", "is_active"),
        CheckConstraint(
            "banner_type IN ('info', 'warning', 'success')",
            name="ck_announcements_banner_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<Announcement id={self.id!r} title={self.title!r} active={self.is_active}>"


# ------------------------------------------------------------------ #
# announcement_config  (single-row global settings for the banner)     #
# ------------------------------------------------------------------ #

class AnnouncementConfigORM(Base):
    __tablename__ = "announcement_config"

    id = Column(Integer, primary_key=True, default=1)
    poll_interval_seconds = Column(Integer, nullable=False, default=15)        # how often users poll for updates
    default_snooze_minutes = Column(Integer, nullable=False, default=30)       # default snooze duration for new announcements
    updated_by = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("id = 1", name="single_row_announcement_config"),
    )

    def __repr__(self) -> str:
        return f"<AnnouncementConfig poll={self.poll_interval_seconds}s snooze={self.default_snooze_minutes}m>"


# ------------------------------------------------------------------ #
# outbox_events  (transactional outbox for domain events)              #
# ------------------------------------------------------------------ #

class OutboxEventORM(Base):
    __tablename__ = "outbox_events"

    id = Column(Text, primary_key=True, default=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    event_type = Column(Text, nullable=False)         # e.g. user.created, user.approved
    # Phase 1.5 §1.5.6 — domain-prefixed event payload contract.
    event_version = Column(Integer, nullable=False, default=1, server_default="1")  # payload schema version
    aggregate_type = Column(Text, nullable=True)      # e.g. "workspace", "ontology"
    aggregate_id = Column(Text, nullable=True)        # the entity id this event refers to
    payload = Column(Text, nullable=False, default="{}")  # JSON
    processed = Column(Boolean, nullable=False, default=False)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_outbox_processed_created", "processed", "created_at"),
        Index("idx_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index("idx_outbox_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return f"<OutboxEvent id={self.id!r} type={self.event_type!r}>"


# ------------------------------------------------------------------ #
# auth_audit_log  (append-only audit trail, drained from the outbox)   #
# ------------------------------------------------------------------ #

class AuthAuditLogORM(Base):
    """Immutable record of every domain event the outbox relay drains.

    Append-only: rows are inserted by the relay and never updated or
    deleted. ``source_event_id`` is the originating outbox event id and
    is UNIQUE so a relay re-run after a crash cannot double-record.
    """
    __tablename__ = "auth_audit_log"

    id = Column(Text, primary_key=True, default=lambda: f"aud_{uuid.uuid4().hex[:12]}")
    source_event_id = Column(Text, nullable=False)   # OutboxEventORM.id
    event_type = Column(Text, nullable=False)
    aggregate_type = Column(Text, nullable=True)
    aggregate_id = Column(Text, nullable=True)
    payload = Column(Text, nullable=False, default="{}")  # JSON (verbatim)
    occurred_at = Column(Text, nullable=False)       # source event created_at
    recorded_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_auth_audit_source_event"),
        Index("idx_auth_audit_event_type", "event_type"),
        Index("idx_auth_audit_recorded_at", "recorded_at"),
    )

    def __repr__(self) -> str:
        return f"<AuthAuditLog id={self.id!r} type={self.event_type!r}>"


# ------------------------------------------------------------------ #
# schema_migrations  (tracks one-time data-fix migrations)            #
# ------------------------------------------------------------------ #

class SchemaMigrationORM(Base):
    __tablename__ = "schema_migrations"

    key = Column(Text, primary_key=True)
    applied_at = Column(Text, nullable=False, default=_now)


# ------------------------------------------------------------------ #
# Cross-domain registration                                             #
# ------------------------------------------------------------------ #
# Domain-owned ORM modules live next to their service code (e.g.,
# AggregationJobORM under services/aggregation/). Import them here so
# `Base.metadata` is fully populated whenever this module is imported —
# Alembic, tests, and any consumer all see the complete schema.
from backend.app.services.aggregation import models as _aggregation_models  # noqa: E402,F401

