"""Widen ``providers.provider_type`` CHECK to include ``spanner``.

Revision ID: 20260508_spanner_provider
Revises: 20260507_jobs_ontology_fp
Create Date: 2026-05-08

Adds Google Spanner Graph as an accepted provider type alongside
falkordb, neo4j, datahub, and mock. The CHECK constraint is the
gate: provider rows with ``provider_type='spanner'`` would otherwise
fail to insert.

PostgreSQL: drop and re-add the constraint.
SQLite: tables are created via ``Base.metadata.create_all`` in the
test environment; the constraint is rebuilt by recreating the table.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260508_spanner_provider"
down_revision: Union[str, None] = "20260507_jobs_ontology_fp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "providers"
_CONSTRAINT = "ck_providers_provider_type"
_NEW_TYPES = ("falkordb", "neo4j", "datahub", "spanner", "mock")
_OLD_TYPES = ("falkordb", "neo4j", "datahub", "mock")


def _types_clause(types) -> str:
    quoted = ", ".join(f"'{t}'" for t in types)
    return f"provider_type IN ({quoted})"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
        op.create_check_constraint(_CONSTRAINT, _TABLE, _types_clause(_NEW_TYPES))
    elif dialect == "sqlite":
        # SQLite cannot ALTER constraints. The test harness rebuilds the
        # schema via ``Base.metadata.create_all`` after model changes, so
        # this migration is a no-op there.
        return
    else:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
        op.create_check_constraint(_CONSTRAINT, _TABLE, _types_clause(_NEW_TYPES))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # Refuse to downgrade if any spanner rows exist; their existence
        # would violate the narrower constraint.
        rows = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE provider_type = 'spanner'")
        ).scalar()
        if rows:
            raise RuntimeError(
                f"Cannot downgrade: {rows} provider row(s) have "
                "provider_type='spanner'. Delete or migrate them first."
            )
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
        op.create_check_constraint(_CONSTRAINT, _TABLE, _types_clause(_OLD_TYPES))
    elif dialect == "sqlite":
        return
    else:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
        op.create_check_constraint(_CONSTRAINT, _TABLE, _types_clause(_OLD_TYPES))
