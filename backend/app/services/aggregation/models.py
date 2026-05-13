"""
Aggregation-owned ORM tables.

These tables live in the ``aggregation`` Postgres schema, fully decoupled
from the viz-service's ``public`` schema.  No foreign keys cross the
schema boundary — data_source_id is a logical reference, not a FK.

Tables:
    aggregation.aggregation_jobs       Job state for crash-recoverable batch materialization.
    aggregation.data_source_state      Per-data-source aggregation status (lightweight).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Index, Integer, Text, text
from backend.app.db.engine import Base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AggregationJobORM(Base):
    """Job tracking table — the worker reads everything from this record.

    All context needed for execution (provider_id, graph_name, edge types)
    is denormalized and frozen at trigger time so the worker is fully
    self-sufficient without querying the public schema.
    """
    __tablename__ = "aggregation_jobs"

    id = Column(Text, primary_key=True, default=lambda: f"agg_{uuid.uuid4().hex[:12]}")

    # ── Logical reference (NOT a FK) — decoupled from public schema ──
    data_source_id = Column(Text, nullable=False, index=True)

    # ── Denormalized context (frozen at trigger time) ────────────────
    # These fields are copied from the viz-service's data at trigger
    # time so the worker and Control Plane never need to JOIN to public.
    workspace_id = Column(Text, nullable=True)
    provider_id = Column(Text, nullable=True)
    graph_name = Column(Text, nullable=True)
    data_source_label = Column(Text, nullable=True)

    ontology_id = Column(Text, nullable=True)  # audit trail — which ontology was used
    projection_mode = Column(Text, nullable=False, default="in_source")
    status = Column(Text, nullable=False, default="pending")
    trigger_source = Column(Text, nullable=False, default="manual")

    # ── Resolved ontology edge types (frozen at trigger time) ────────
    containment_edge_types = Column(Text, nullable=True)  # JSON: ["CONTAINS", "HAS_SCHEMA"]
    lineage_edge_types = Column(Text, nullable=True)  # JSON: ["FLOWS_TO", "TRANSFORMS"]

    # ── Progress tracking (cursor-based checkpoint) ──────────────────
    progress = Column(Integer, nullable=False, default=0)  # 0-100
    total_edges = Column(Integer, nullable=False, default=0)
    processed_edges = Column(Integer, nullable=False, default=0)
    created_edges = Column(Integer, nullable=False, default=0)  # AGGREGATED edges created
    last_cursor = Column(Text, nullable=True)  # cursor-based resume point (NOT offset)
    batch_size = Column(Integer, nullable=False, default=1000)
    last_checkpoint_at = Column(Text, nullable=True)

    # ── Error handling ───────────────────────────────────────────────
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=3)

    # ── Phase visibility (Phase 1.7) ─────────────────────────────────
    # Short string identifying which phase of the bulk-rebuild path is
    # currently running. Surfaced to the UI so operators can see why
    # FalkorDB has zero edges during the scan window even though the
    # job is making progress. Values: 'wiping' / 'scanning' /
    # 'resolving_labels' / 'creating' / 'finalizing'. NULL on legacy
    # rows and on paths (Neo4j/Spanner/legacy MERGE) that don't emit
    # phase signals — frontend falls back to a generic label.
    current_phase = Column(Text, nullable=True)

    # ── Dynamic timeout (estimated from graph size at trigger time) ──
    timeout_secs = Column(Integer, nullable=True)  # None = use global default

    # ── Fingerprinting (change detection) ────────────────────────────
    graph_fingerprint_before = Column(Text, nullable=True)
    graph_fingerprint_after = Column(Text, nullable=True)

    # ── Idempotency ─────────────────────────────────────────────────
    idempotency_key = Column(Text, nullable=True)

    # Ontology resolution fingerprint at trigger time. Stable hash over
    # the assigned ontology's revision + entity / relationship type
    # definitions (computed by ``backend.app.ontology.gate``). The
    # idempotency replay (60-min window keyed by idempotency_key) only
    # short-circuits when this matches the current ontology fingerprint;
    # any change to containment / lineage classifications shifts the
    # fingerprint and forces a fresh resolve. NULL on rows created
    # before this column existed and on aggregation_data_source_state
    # entries cleared by the ontology PUT invalidation hook — both are
    # treated as "stale, recompute".
    ontology_fingerprint = Column(Text, nullable=True)

    # Per-emit sequence counter for the platform JobEmitter. Strictly
    # monotonic per job_id. The worker increments it on every progress
    # / heartbeat / terminal event published, and persists the highest
    # value at outer-batch boundaries. On crash + resume, the recovered
    # worker reads ``last_sequence`` and continues numbering from there
    # so downstream consumers (SSE clients, audit log) can detect gaps
    # and dedup retried events via ``(job_id, sequence)``. Nullable for
    # back-compat with rows created before this column existed; treat
    # NULL as 0.
    last_sequence = Column(Integer, nullable=True, default=0)

    # ── Timestamps ───────────────────────────────────────────────────
    started_at = Column(Text, nullable=True)
    completed_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("ix_agg_jobs_ds_status", "data_source_id", "status"),
        Index("ix_agg_jobs_created_at", "created_at"),
        Index("ix_agg_jobs_workspace", "workspace_id"),
        Index(
            "ix_agg_jobs_idem_active",
            "data_source_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_agg_jobs_status",
        ),
        CheckConstraint(
            "trigger_source IN ('onboarding', 'manual', 'schedule', 'drift', 'api', 'purge')",
            name="ck_agg_jobs_trigger_source",
        ),
        CheckConstraint(
            "projection_mode IN ('in_source', 'dedicated')",
            name="ck_agg_jobs_projection_mode",
        ),
        {"schema": "aggregation"},
    )


class AggregationDataSourceStateORM(Base):
    """Lightweight per-data-source aggregation state.

    Replaces the aggregation-specific columns that were previously
    stored on ``public.workspace_data_sources``.  The Control Plane
    reads/writes this table.  The viz-service syncs its own copy
    via Redis events.
    """
    __tablename__ = "data_source_state"
    __table_args__ = ({"schema": "aggregation"},)

    data_source_id = Column(Text, primary_key=True)
    workspace_id = Column(Text, nullable=False, index=True)
    aggregation_status = Column(Text, nullable=False, default="none")
    last_aggregated_at = Column(Text, nullable=True)
    aggregation_edge_count = Column(Integer, nullable=False, default=0)
    graph_fingerprint = Column(Text, nullable=True)
    aggregation_schedule = Column(Text, nullable=True)  # cron expression
