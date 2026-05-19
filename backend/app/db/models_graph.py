"""ORM models for the **Graph Store DB** — the decoupled, durable
system-of-record for user-authored, versioned graphs.

Bound to :class:`backend.app.db.graph_store_engine.GraphStoreBase`
(NOT the management ``Base``) so this schema is structurally isolated
from the management database.

House conventions kept: type-prefixed ``uuid4().hex[:12]`` ids; ISO
``_now()`` text timestamps for audit/wall-clock columns; ``CheckConstraint``
enums; explicit ``Index`` objects.

Deliberate, plan-approved deviations from the management-DB house style
(both are load-bearing and safe — the Graph Store is Postgres-only):

* ``JSONB`` (not TEXT-encoded JSON) for property/diff payloads so diff
  and blame can query inside them.
* A real monotonic ``BigInteger`` identity (``commit_seq``) for commit
  total-ordering instead of a sortable text timestamp — delta/diff
  correctness and partition pruning depend on a true total order.

Cross-DB references (``workspace_id``, ``ontology_id``, ``created_by``,
``user_id`` …) are plain id strings — **no ForeignKey across the
database boundary**. FKs below are only ever within this database.

Partitioning note: the high-volume tables (``graph_node_versions``,
``graph_edge_versions``, ``graph_change_event``, ``graph_commits``) are
declared with composite primary keys that include ``graph_id`` so the
dedicated Alembic lineage can convert them to ``PARTITION BY LIST
(graph_id)`` without a PK change. The metadata-create_all dev fallback
produces the same (un-partitioned) logical schema.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .graph_store_engine import GraphStoreBase


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str):
    return lambda: f"{prefix}_{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# user_graphs — one row per user-authored versioned graph              #
# ------------------------------------------------------------------ #

class UserGraphORM(GraphStoreBase):
    """A versioned graph (the "repository"). Soft-deletable,
    optimistic-locked.

    ``origin`` makes the model work for **both** kinds of graph under
    one identical version-control engine:

    * ``authored``  — created from scratch in the editor. Default
      ``schemaless`` (``ontology_id`` NULL).
    * ``connected`` — adopted from an existing provider data source via
      a one-time genesis-import (provider → first commit; the inverse of
      materialization). ``source_data_source_id`` records which
      management ``workspace_data_sources`` row it was adopted from;
      typically ``strict`` with an ``ontology_id``. Subsequent upstream
      connector/aggregation refreshes land as automated commits on the
      reserved ``upstream`` branch; users edit ``main`` and merge
      ``upstream → main`` via the normal three-way merge.

    A data source is only adopted on an explicit opt-in action — until
    then connected graphs keep the untouched live-provider path.
    ``ontology_id`` is NULL for ``schemaless`` graphs (default) and set
    when ``strict``.
    """

    __tablename__ = "user_graphs"

    id = Column(Text, primary_key=True, default=_id("g"))
    workspace_id = Column(Text, nullable=False)          # soft ref → management.workspaces
    ontology_id = Column(Text, nullable=True)            # soft ref → management.ontologies
    origin = Column(Text, nullable=False, default="authored")
    source_data_source_id = Column(Text, nullable=True)  # soft ref → management.workspace_data_sources (connected only)
    # Fork provenance (origin='fork' only): the base graph this was
    # forked from and the base commit it was taken at (= the permanent
    # three-way merge base for every PR raised from this fork).
    forked_from_graph_id = Column(Text, nullable=True)   # soft ref → user_graphs.id (same Graph Store DB)
    fork_point_commit_id = Column(Text, nullable=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    schema_mode = Column(Text, nullable=False, default="schemaless")
    default_branch = Column(Text, nullable=False, default="main")
    partition_count = Column(Integer, nullable=False, default=4096)  # frozen at create
    version = Column(Integer, nullable=False, default=0)             # optimistic lock
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now)
    deleted_at = Column(Text, nullable=True)
    deleted_by = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_user_graphs_workspace", "workspace_id"),
        Index("idx_user_graphs_deleted", "deleted_at"),
        Index("idx_user_graphs_source_ds", "source_data_source_id"),
        Index("idx_user_graphs_forked_from", "forked_from_graph_id"),
        CheckConstraint(
            "schema_mode IN ('schemaless', 'strict')",
            name="ck_user_graphs_schema_mode",
        ),
        CheckConstraint(
            "origin IN ('authored', 'connected', 'fork')",
            name="ck_user_graphs_origin",
        ),
    )


# ------------------------------------------------------------------ #
# graph_refs — mutable branch/tag pointers (optimistic-locked)         #
# ------------------------------------------------------------------ #

class GraphRefORM(GraphStoreBase):
    """A named ref (branch or tag) pointing at a commit. ``revision``
    is the optimistic lock guarding ref advance (mirrors
    ``OntologyORM.revision``)."""

    __tablename__ = "graph_refs"

    id = Column(Text, primary_key=True, default=_id("gref"))
    graph_id = Column(Text, ForeignKey("user_graphs.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    ref_type = Column(Text, nullable=False, default="branch")
    commit_id = Column(Text, nullable=True)              # NULL until first commit
    revision = Column(Integer, nullable=False, default=0)
    is_protected = Column(Boolean, nullable=False, default=False)
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("graph_id", "name", name="uq_graph_refs_graph_name"),
        Index("idx_graph_refs_graph", "graph_id"),
        CheckConstraint(
            "ref_type IN ('branch', 'tag')", name="ck_graph_refs_type"
        ),
    )


# ------------------------------------------------------------------ #
# graph_commits — immutable commit DAG                                 #
# ------------------------------------------------------------------ #

class GraphCommitORM(GraphStoreBase):
    """Immutable commit. ``parent_ids`` is a JSON list (0 = root,
    1 = normal, 2 = merge). ``commit_seq`` is a true monotonic order
    within a graph (load-bearing for diff/partition pruning). No
    ``updated_at``/``deleted_at`` — commits are immutable."""

    __tablename__ = "graph_commits"

    id = Column(Text, primary_key=True, default=_id("gcmt"))
    graph_id = Column(Text, nullable=False)
    commit_seq = Column(BigInteger, nullable=False, autoincrement=True)
    commit_hash = Column(Text, nullable=False)
    parent_ids = Column(JSONB, nullable=False, default=list)
    merge_base_id = Column(Text, nullable=True)
    root_manifest_hash = Column(Text, nullable=False)
    author = Column(Text, nullable=True)
    message = Column(Text, nullable=True)
    ontology_digest = Column(Text, nullable=True)
    delta_summary = Column(JSONB, nullable=False, default=dict)
    committed_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        # (graph_id, commit_hash) is the content-addressed identity. The
        # dedicated Alembic lineage recreates this table PARTITION BY
        # LIST(graph_id) with a composite PK; the ORM keeps the surrogate
        # `id` PK for the logical schema / create_all fallback.
        UniqueConstraint("graph_id", "commit_hash", name="uq_graph_commits_hash"),
        Index("idx_graph_commits_graph_seq", "graph_id", "commit_seq"),
    )


# ------------------------------------------------------------------ #
# graph_node_versions / graph_edge_versions — content-addressed blobs  #
# ------------------------------------------------------------------ #

class GraphNodeVersionORM(GraphStoreBase):
    """Immutable, content-addressed node version. Dedup key is
    ``(graph_id, content_hash)`` — editing one node in a million-node
    graph inserts exactly one row. ``content_hash`` covers
    ``position`` (layout is versioned)."""

    __tablename__ = "graph_node_versions"

    id = Column(Text, primary_key=True, default=_id("gnv"))
    graph_id = Column(Text, nullable=False)
    node_key = Column(Text, nullable=False)              # stable logical id (urn)
    content_hash = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=True)
    display_name = Column(Text, nullable=True)
    position = Column(JSONB, nullable=True)              # {x, y} — versioned
    properties = Column(JSONB, nullable=False, default=dict)
    tags = Column(JSONB, nullable=False, default=list)
    is_cold = Column(Boolean, nullable=False, default=False)  # cold-tier flag
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("graph_id", "content_hash", name="uq_gnv_graph_content"),
        Index("idx_gnv_graph_nodekey", "graph_id", "node_key"),
    )


class GraphEdgeVersionORM(GraphStoreBase):
    """Immutable, content-addressed edge version. An endpoint change is
    a new content version of the same ``edge_key``."""

    __tablename__ = "graph_edge_versions"

    id = Column(Text, primary_key=True, default=_id("gev"))
    graph_id = Column(Text, nullable=False)
    edge_key = Column(Text, nullable=False)
    content_hash = Column(Text, nullable=False)
    source_node_key = Column(Text, nullable=False)
    target_node_key = Column(Text, nullable=False)
    edge_type = Column(Text, nullable=True)
    confidence = Column(Text, nullable=True)
    properties = Column(JSONB, nullable=False, default=dict)
    is_cold = Column(Boolean, nullable=False, default=False)
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("graph_id", "content_hash", name="uq_gev_graph_content"),
        Index("idx_gev_graph_src", "graph_id", "source_node_key"),
        Index("idx_gev_graph_tgt", "graph_id", "target_node_key"),
    )


# ------------------------------------------------------------------ #
# graph_partition_manifest — content-addressed 2-level Merkle tree     #
# ------------------------------------------------------------------ #

class GraphPartitionManifestORM(GraphStoreBase):
    """A content-addressed manifest node. ``partition_index = -1`` is
    the root manifest (lists the N partition hashes); otherwise a leaf
    partition manifest (gzip-canonical ``(id, kind, content_hash)[]``).
    Unchanged manifests are shared by hash across commits and branches
    (structural sharing → O(changes) commits/diffs)."""

    __tablename__ = "graph_partition_manifest"

    manifest_hash = Column(Text, primary_key=True)
    graph_id = Column(Text, nullable=False)
    partition_index = Column(Integer, nullable=False)
    entries = Column(LargeBinary, nullable=False)        # gzip canonical payload
    entry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gpm_graph_partition", "graph_id", "partition_index"),
    )


# ------------------------------------------------------------------ #
# graph_change_event — the single immutable per-attribute audit stream #
# ------------------------------------------------------------------ #

class GraphChangeEventORM(GraphStoreBase):
    """Immutable, append-only audit. One row per object create/delete
    and one per changed attribute on update. Written at *stage* time
    (uncommitted edits are still trailed); ``commit_id`` is stamped on
    commit. Covers CRUD *and* lifecycle actions. Mirrors
    ``OntologyAuditLogORM`` immutability + index shape."""

    __tablename__ = "graph_change_event"

    id = Column(Text, primary_key=True, default=_id("gce"))
    graph_id = Column(Text, nullable=False)
    branch = Column(Text, nullable=False)
    commit_id = Column(Text, nullable=True)              # NULL while staged
    object_kind = Column(Text, nullable=False)           # node | edge | graph | branch
    object_id = Column(Text, nullable=True)
    action = Column(Text, nullable=False)
    attribute_path = Column(Text, nullable=True)
    old_value = Column(JSONB, nullable=True)
    new_value = Column(JSONB, nullable=True)
    prev_content_hash = Column(Text, nullable=True)
    new_content_hash = Column(Text, nullable=True)
    actor = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gce_blame", "graph_id", "object_kind", "object_id", "created_at"),
        Index("idx_gce_commit", "graph_id", "commit_id"),
        Index("idx_gce_branch", "graph_id", "branch", "created_at"),
        Index("idx_gce_actor", "actor", "action", "created_at"),
        CheckConstraint(
            "object_kind IN ('node', 'edge', 'graph', 'branch')",
            name="ck_gce_object_kind",
        ),
        CheckConstraint(
            "action IN ('created', 'updated', 'deleted', 'restored', "
            "'committed', 'branched', 'merged', 'reverted', 'materialized')",
            name="ck_gce_action",
        ),
    )


# ------------------------------------------------------------------ #
# graph_working_set / graph_working_change — Git-style per-user index  #
# ------------------------------------------------------------------ #

class GraphWorkingSetORM(GraphStoreBase):
    """Per-(graph, branch, user) uncommitted working copy (the index).
    Git-style isolation: each user has their own working set on a
    branch."""

    __tablename__ = "graph_working_set"

    id = Column(Text, primary_key=True, default=_id("gws"))
    graph_id = Column(Text, nullable=False)
    branch = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False)
    base_commit_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="open")
    ws_change_version = Column(Integer, nullable=False, default=0)  # coarse guard
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "graph_id", "branch", "user_id", name="uq_gws_graph_branch_user"
        ),
        Index("idx_gws_graph_branch", "graph_id", "branch"),
        CheckConstraint(
            "status IN ('open', 'committing', 'abandoned')",
            name="ck_gws_status",
        ),
    )


class GraphWorkingChangeORM(GraphStoreBase):
    """A single staged op in a working set. ``base_content_hash`` is
    the lost-update guard checked at commit; ``seq`` drives apply
    order."""

    __tablename__ = "graph_working_change"

    id = Column(Text, primary_key=True, default=_id("gwc"))
    working_set_id = Column(
        Text, ForeignKey("graph_working_set.id", ondelete="CASCADE"), nullable=False
    )
    change_type = Column(Text, nullable=False)
    object_kind = Column(Text, nullable=False)           # node | edge
    object_id = Column(Text, nullable=False)             # real id or staged_ temp id
    base_content_hash = Column(Text, nullable=True)
    before_blob = Column(JSONB, nullable=True)
    after_blob = Column(JSONB, nullable=True)
    summary = Column(Text, nullable=False, default="")
    seq = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gwc_ws_seq", "working_set_id", "seq"),
        CheckConstraint(
            "object_kind IN ('node', 'edge')", name="ck_gwc_object_kind"
        ),
    )


# ------------------------------------------------------------------ #
# graph_merge / graph_merge_conflict — three-way merge state           #
# ------------------------------------------------------------------ #

class GraphMergeORM(GraphStoreBase):
    """One merge attempt. ``status`` walks open → resolved → committed
    (or aborted)."""

    __tablename__ = "graph_merge"

    id = Column(Text, primary_key=True, default=_id("gmrg"))
    graph_id = Column(Text, nullable=False)
    source_branch = Column(Text, nullable=False)
    target_branch = Column(Text, nullable=False)
    base_commit_id = Column(Text, nullable=True)
    source_commit_id = Column(Text, nullable=True)
    target_commit_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="open")
    result_commit_id = Column(Text, nullable=True)
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gmrg_graph_status", "graph_id", "status"),
        CheckConstraint(
            "status IN ('open', 'resolved', 'aborted', 'committed')",
            name="ck_gmrg_status",
        ),
    )


class GraphMergeConflictORM(GraphStoreBase):
    """One conflicting object/attribute in a merge. ``dangling_edge``
    and ``structural`` conflicts are never auto-resolved."""

    __tablename__ = "graph_merge_conflict"

    id = Column(Text, primary_key=True, default=_id("gmc"))
    merge_id = Column(
        Text, ForeignKey("graph_merge.id", ondelete="CASCADE"), nullable=False
    )
    conflict_class = Column(Text, nullable=False)
    object_kind = Column(Text, nullable=False)
    object_id = Column(Text, nullable=False)
    attribute_path = Column(Text, nullable=True)
    base_value = Column(JSONB, nullable=True)
    source_value = Column(JSONB, nullable=True)
    target_value = Column(JSONB, nullable=True)
    resolution = Column(Text, nullable=True)
    resolved_value = Column(JSONB, nullable=True)
    resolved_by = Column(Text, nullable=True)
    resolved_at = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_gmc_merge", "merge_id"),
        Index("idx_gmc_merge_class", "merge_id", "conflict_class"),
        CheckConstraint(
            "conflict_class IN ('attr', 'add_add', 'edit_delete', "
            "'dangling_edge', 'edge_endpoint', 'structural')",
            name="ck_gmc_class",
        ),
    )


# ------------------------------------------------------------------ #
# outbox_events — the Graph Store's OWN transactional outbox            #
# ------------------------------------------------------------------ #

class GraphStoreOutboxEventORM(GraphStoreBase):
    """The Graph Store DB's own outbox. Co-located here (not in the
    management DB) so a graph write + its audit row + the event commit
    atomically in a single local transaction — this is the structural
    fix for cross-DB atomicity (no transaction ever spans both
    databases). A dedicated relay drains this to Redis Streams.

    Same row shape + ``<domain>.<entity>.<verb>`` contract as
    ``backend.app.db.models.OutboxEventORM`` so the existing
    ``_VALID_DOMAINS`` / event-type validation can be reused unchanged
    (events emitted under the already-whitelisted ``visualization``
    domain)."""

    __tablename__ = "outbox_events"

    id = Column(Text, primary_key=True, default=_id("evt"))
    event_type = Column(Text, nullable=False)
    event_version = Column(Integer, nullable=False, default=1)
    aggregate_type = Column(Text, nullable=True)
    aggregate_id = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False, default=dict)
    processed = Column(Boolean, nullable=False, default=False)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gs_outbox_processed_created", "processed", "created_at"),
        Index("idx_gs_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index("idx_gs_outbox_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return f"<GraphStoreOutboxEvent id={self.id!r} type={self.event_type!r}>"


# ------------------------------------------------------------------ #
# Pull requests — a reviewable merge request from a fork (or branch)   #
# ------------------------------------------------------------------ #

class GraphPullRequestORM(GraphStoreBase):
    """A reviewable request to merge ``head_graph_id@head_ref`` into
    ``base_graph_id@base_branch``. For fork PRs head_graph_id != base
    (cross-graph, possibly cross-workspace); for in-graph branch PRs
    they are equal. ``merge_base_commit_id`` is the fixed three-way
    merge base (the fork point, or the branch LCA). Merge is gated on
    an ``approved`` review by someone with merge rights on the BASE."""

    __tablename__ = "graph_pull_request"

    id = Column(Text, primary_key=True, default=_id("gpr"))
    base_graph_id = Column(Text, nullable=False)
    base_branch = Column(Text, nullable=False)
    head_graph_id = Column(Text, nullable=False)
    head_ref = Column(Text, nullable=False)
    merge_base_commit_id = Column(Text, nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="open")
    merged_commit_id = Column(Text, nullable=True)
    created_by = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)
    updated_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gpr_base", "base_graph_id", "status"),
        Index("idx_gpr_head", "head_graph_id"),
        Index("idx_gpr_created_by", "created_by"),
        CheckConstraint(
            "status IN ('open', 'changes_requested', 'approved', "
            "'merged', 'closed')",
            name="ck_gpr_status",
        ),
    )


class GraphPrReviewORM(GraphStoreBase):
    """One review verdict on a PR. The most recent review per reviewer
    is authoritative; merge requires at least one ``approved`` with no
    later ``changes_requested`` (policy enforced in the service)."""

    __tablename__ = "graph_pr_review"

    id = Column(Text, primary_key=True, default=_id("gprv"))
    pr_id = Column(
        Text, ForeignKey("graph_pull_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer = Column(Text, nullable=True)
    state = Column(Text, nullable=False)
    body = Column(Text, nullable=True)
    # The (base_head, head_head) the verdict was given against — used to
    # invalidate stale approvals when either side advances.
    reviewed_base_commit_id = Column(Text, nullable=True)
    reviewed_head_commit_id = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_gprv_pr", "pr_id", "created_at"),
        CheckConstraint(
            "state IN ('approved', 'changes_requested', 'commented')",
            name="ck_gprv_state",
        ),
    )


class GraphPrCommentORM(GraphStoreBase):
    """A general PR conversation comment (inline per-object review is a
    later, explicitly-deferred enhancement)."""

    __tablename__ = "graph_pr_comment"

    id = Column(Text, primary_key=True, default=_id("gprc"))
    pr_id = Column(
        Text, ForeignKey("graph_pull_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    author = Column(Text, nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False, default=_now)

    __table_args__ = (Index("idx_gprc_pr", "pr_id", "created_at"),)


__all__ = [
    "UserGraphORM",
    "GraphRefORM",
    "GraphCommitORM",
    "GraphNodeVersionORM",
    "GraphEdgeVersionORM",
    "GraphPartitionManifestORM",
    "GraphChangeEventORM",
    "GraphWorkingSetORM",
    "GraphWorkingChangeORM",
    "GraphMergeORM",
    "GraphMergeConflictORM",
    "GraphStoreOutboxEventORM",
    "GraphPullRequestORM",
    "GraphPrReviewORM",
    "GraphPrCommentORM",
]
