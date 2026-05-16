"""Graph Store baseline — create the versioned-graph schema.

Revision ID: 0001_graph_store_baseline
Revises:
Create Date: 2026-05-16

Single baseline that creates ALL Graph Store tables from the isolated
``GraphStoreBase`` metadata (the 12-table system-of-record: user_graphs,
graph_refs, graph_commits, graph_node_versions, graph_edge_versions,
graph_partition_manifest, graph_change_event, graph_working_set,
graph_working_change, graph_merge, graph_merge_conflict, and the Graph
Store's own outbox_events).

Mirrors the management ``0001_baseline`` approach (metadata
``create_all``) so a clone-and-run dev workflow is one command.

Scope note: the high-volume tables (graph_node_versions,
graph_edge_versions, graph_change_event, graph_commits) are created as
plain logical tables here. Converting them to ``PARTITION BY LIST
(graph_id)`` with composite keys, plus dynamic per-graph partition
creation, is a dedicated Phase-3 hardening migration (tracked in
docs/GRAPH_AUTHORING_VERSIONING_STRATEGY.md) — deliberately not shipped
in the baseline so no unvalidated partition DDL lands before it can be
exercised against a real Postgres.

Dev workflow:
    # ensure the separate graph-store database exists, then:
    cd backend && alembic -c alembic_graph_store.ini upgrade head
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from backend.app.db.graph_store_engine import GraphStoreBase
from backend.app.db import models_graph as _models_graph  # noqa: F401


revision: str = "0001_graph_store_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Create every Graph Store table from current ORM state. The
    # isolated metadata guarantees only graph-store tables are touched
    # (it shares nothing with the management Base).
    GraphStoreBase.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    GraphStoreBase.metadata.drop_all(bind=bind)
