"""Graph Store: fork provenance + pull-request tables (Phase 2.5).

Revision ID: 0002_fork_and_pr
Revises: 0001_graph_store_baseline
Create Date: 2026-05-16

- user_graphs: allow origin='fork'; add forked_from_graph_id +
  fork_point_commit_id (+ index). The provenance link / fixed merge
  base for every PR raised from a fork.
- new tables: graph_pull_request, graph_pr_review, graph_pr_comment
  (the reviewable merge request).

Idempotency: column adds + index use IF NOT EXISTS; the origin CHECK is
dropped/recreated; tables created via the isolated GraphStoreBase
metadata with checkfirst (mirrors the baseline's create_all approach).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from backend.app.db.graph_store_engine import GraphStoreBase
from backend.app.db import models_graph as _m  # noqa: F401 — register tables


revision: str = "0002_fork_and_pr"
down_revision: Union[str, None] = "0001_graph_store_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = ("graph_pull_request", "graph_pr_review", "graph_pr_comment")


def upgrade() -> None:
    bind = op.get_bind()

    # 1. user_graphs provenance columns + index (idempotent).
    op.execute(
        "ALTER TABLE user_graphs "
        "ADD COLUMN IF NOT EXISTS forked_from_graph_id text"
    )
    op.execute(
        "ALTER TABLE user_graphs "
        "ADD COLUMN IF NOT EXISTS fork_point_commit_id text"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_graphs_forked_from "
        "ON user_graphs (forked_from_graph_id)"
    )

    # 2. Widen the origin CHECK to include 'fork'.
    op.execute(
        "ALTER TABLE user_graphs DROP CONSTRAINT IF EXISTS ck_user_graphs_origin"
    )
    op.execute(
        "ALTER TABLE user_graphs ADD CONSTRAINT ck_user_graphs_origin "
        "CHECK (origin IN ('authored', 'connected', 'fork'))"
    )

    # 3. Create the PR tables from the isolated metadata (checkfirst so
    #    re-runs are safe; only the 3 new tables are touched).
    md = GraphStoreBase.metadata
    md.create_all(
        bind=bind,
        tables=[md.tables[t] for t in _NEW_TABLES],
        checkfirst=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    md = GraphStoreBase.metadata
    md.drop_all(bind=bind, tables=[md.tables[t] for t in _NEW_TABLES])
    op.execute(
        "ALTER TABLE user_graphs DROP CONSTRAINT IF EXISTS ck_user_graphs_origin"
    )
    op.execute(
        "ALTER TABLE user_graphs ADD CONSTRAINT ck_user_graphs_origin "
        "CHECK (origin IN ('authored', 'connected'))"
    )
    op.execute("DROP INDEX IF EXISTS idx_user_graphs_forked_from")
    op.execute("ALTER TABLE user_graphs DROP COLUMN IF EXISTS fork_point_commit_id")
    op.execute("ALTER TABLE user_graphs DROP COLUMN IF EXISTS forked_from_graph_id")
