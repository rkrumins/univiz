"""Add ``ontology_fingerprint`` column to ``aggregation.aggregation_jobs``.

Revision ID: 20260507_jobs_ontology_fp
Revises: 20260430_1900_access_requests
Create Date: 2026-05-07 11:00

Backs the ontology-aware idempotency replay introduced together with the
ontology-resolution gate. The 60-minute idempotency window
(``service.trigger`` lines 96-117) used to return the prior job
unconditionally, freezing whatever ``containment_edge_types`` /
``lineage_edge_types`` were resolved at first trigger. Editing the
ontology between runs left the replay stale.

The new column stores a stable hash over the ontology revision plus
its entity / relationship definitions at trigger time. The replay
lookup additionally requires a fingerprint match; otherwise the prior
job is treated as stale and a fresh resolve runs.

Nullable + no default so existing rows keep behaving as "stale,
recompute" (the replay equality fails when one side is NULL). No
backfill required.

Idempotent: the baseline migration uses ``Base.metadata.create_all``
so on fresh deploys the column may already exist. Inspector check
before add_column.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260507_jobs_ontology_fp"
down_revision: Union[str, None] = "20260430_1900_access_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "aggregation_jobs"
_SCHEMA = "aggregation"
_COLUMN = "ontology_fingerprint"


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
