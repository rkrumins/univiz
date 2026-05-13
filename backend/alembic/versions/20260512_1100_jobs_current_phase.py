"""Add ``current_phase`` column to ``aggregation.aggregation_jobs``.

Revision ID: 20260512_1100_jobs_current_phase
Revises: 20260508_spanner_provider
Create Date: 2026-05-12 11:00

Backs Phase 1.7 of the aggregation hardening (see plan
``i-want-you-to-merry-minsky.md``). The bulk-rebuild path has five
internal phases — wipe, scan, label-resolve, create, finalize — and
operators have repeatedly reported "nothing is being written to
FalkorDB" during the long no-write window of phases A+B+C. The new
column carries a short phase ID set by the worker's checkpoint
closure; the UI's ``JobRow`` resolves it to an operator-readable
status label.

Without this migration, the viz-service errors at startup with
``UndefinedColumnError: column aggregation_jobs.current_phase does
not exist`` because the ORM declares the column and SQLAlchemy
auto-includes it in every SELECT generated from the model.

Nullable + no default. Legacy rows and paths that don't emit phase
signals (Neo4j, Spanner, legacy MERGE-based aggregation) leave the
column NULL — the frontend treats NULL as "render the generic
'Processing lineage edges' label", so no backfill is required.

Idempotent: the baseline migration uses ``Base.metadata.create_all``
so on fresh deploys the column may already exist (Phase 1.7 also
adds an idempotent ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` to
``backend/app/services/aggregation/db_init.py`` for the
Worker / Control-Plane init path). Inspector check before
``add_column`` makes the Alembic upgrade safe on databases that
already have the column from either path.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260512_1100_jobs_current_phase"
down_revision: Union[str, None] = "20260508_spanner_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "aggregation_jobs"
_SCHEMA = "aggregation"
_COLUMN = "current_phase"


def _has_column(inspector: sa.engine.reflection.Inspector) -> bool:
    columns = inspector.get_columns(_TABLE, schema=_SCHEMA)
    return any(c["name"] == _COLUMN for c in columns)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Text(), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector):
        return
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)
